"""Parse and normalize the doubly-nested telemetry JSONL.

The telemetry file is NOT one event per line. Each line is a CloudWatch-style
*batch*, and the real events live as JSON *strings* inside ``logEvents[].message``:

    line (batch): { ..., logEvents: [ { id, timestamp, message } ] }
        message (JSON string): { body, attributes, scope, resource }

This module streams the file line by line, unpacks both layers, flattens each
event into one normalized dict with typed values, and reports validation stats.
It has zero third-party dependencies so it can be unit-tested in isolation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Attribute keys whose values are numeric but stored as strings in the raw data.
_INT_FIELDS = {
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "duration_ms",
    "prompt_length",
    "tool_result_size_bytes",
    "attempt",
}
_FLOAT_FIELDS = {"cost_usd"}


@dataclass
class IngestStats:
    """Counts collected during a pass over the telemetry file."""

    batches: int = 0
    events: int = 0
    skipped_lines: int = 0          # line was not valid JSON
    skipped_events: int = 0         # event message failed to parse / validate
    by_event_type: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # --- multi-provider extensions (all optional; Claude-Code-only callers can ignore) ---
    records: int = 0                # raw source records seen (log events / lines / CSV rows)
    duplicates: int = 0             # rows dropped by (provider, source_event_id) dedup
    by_provider: dict[str, int] = field(default_factory=dict)
    unknown_models: set[str] = field(default_factory=set)   # "provider:model_family" w/o a price

    def note_type(self, event_type: str) -> None:
        self.by_event_type[event_type] = self.by_event_type.get(event_type, 0) + 1

    def note_provider(self, provider: str, n: int = 1) -> None:
        self.by_provider[provider] = self.by_provider.get(provider, 0) + n

    def warn(self, message: str) -> None:
        """Record a parse/validation warning (capped so a bad file can't flood memory)."""
        if len(self.errors) < 20:
            self.errors.append(message)

    def merge(self, other: "IngestStats") -> None:
        """Fold another (e.g. per-provider) stats object into this one."""
        self.batches += other.batches
        self.events += other.events
        self.skipped_lines += other.skipped_lines
        self.skipped_events += other.skipped_events
        self.records += other.records
        self.duplicates += other.duplicates
        for k, v in other.by_event_type.items():
            self.by_event_type[k] = self.by_event_type.get(k, 0) + v
        for k, v in other.by_provider.items():
            self.note_provider(k, v)
        self.unknown_models |= other.unknown_models
        for msg in other.errors:
            self.warn(msg)


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(raw: str | None) -> datetime | None:
    """Parse the ISO-8601 ``event.timestamp`` (millisecond precision, trailing Z)."""
    if not raw:
        return None
    try:
        # Python's fromisoformat handles the offset once we swap the trailing Z.
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def normalize_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten one raw event object into a single typed row.

    Returns ``None`` if the event lacks the fields every downstream query relies
    on (a parseable timestamp and a session id).
    """
    attrs = event.get("attributes") or {}
    resource = event.get("resource") or {}
    scope = event.get("scope") or {}

    ts = _parse_timestamp(attrs.get("event.timestamp"))
    session_id = attrs.get("session.id")
    if ts is None or not session_id:
        return None

    # body looks like "claude_code.api_request" -> event_type "api_request"
    body = event.get("body", "")
    event_type = body.split(".", 1)[1] if "." in body else body

    row: dict[str, Any] = {
        # --- common dimensions ---
        "event_type": event_type,
        "timestamp": ts,
        "session_id": session_id,
        "user_email": attrs.get("user.email"),
        "user_id": attrs.get("user.id"),
        "account_uuid": attrs.get("user.account_uuid"),
        "org_id": attrs.get("organization.id"),
        "terminal_type": attrs.get("terminal.type"),
        # --- environment (from resource / scope) ---
        "cli_version": resource.get("service.version") or scope.get("version"),
        "os_type": resource.get("os.type"),
        "os_version": resource.get("os.version"),
        "host_arch": resource.get("host.arch"),
        "resource_practice": resource.get("user.practice"),
        # --- type-specific (nullable depending on event_type) ---
        "model": attrs.get("model"),
        "cost_usd": _to_float(attrs.get("cost_usd")),
        "duration_ms": _to_int(attrs.get("duration_ms")),
        "input_tokens": _to_int(attrs.get("input_tokens")),
        "output_tokens": _to_int(attrs.get("output_tokens")),
        "cache_read_tokens": _to_int(attrs.get("cache_read_tokens")),
        "cache_creation_tokens": _to_int(attrs.get("cache_creation_tokens")),
        "tool_name": attrs.get("tool_name"),
        "decision": attrs.get("decision"),
        "decision_source": attrs.get("source") or attrs.get("decision_source"),
        "decision_type": attrs.get("decision_type"),
        "success": {"true": True, "false": False}.get(str(attrs.get("success")).lower())
        if attrs.get("success") is not None
        else None,
        "tool_result_size_bytes": _to_int(attrs.get("tool_result_size_bytes")),
        "prompt_length": _to_int(attrs.get("prompt_length")),
        "error": attrs.get("error"),
        "status_code": attrs.get("status_code"),
        "attempt": _to_int(attrs.get("attempt")),
    }
    return row


def iter_events(jsonl_path: str | Path, stats: IngestStats | None = None) -> Iterator[dict[str, Any]]:
    """Yield one normalized event dict per telemetry event.

    Malformed batches and events are skipped and tallied in ``stats`` (create one
    and pass it in to inspect the counts afterward). Streaming keeps memory flat
    regardless of file size.
    """
    for _event_id, row in iter_log_events(jsonl_path, stats):
        yield row


def iter_log_events(
    jsonl_path: str | Path, stats: IngestStats | None = None
) -> Iterator[tuple[str | None, dict[str, Any]]]:
    """Like :func:`iter_events` but also yields the CloudWatch ``logEvents[].id``.

    The id is the natural de-duplication key for a log event (the multi-provider
    ETL stores it as ``source_event_id``). Yields ``(log_event_id, row)``.
    """
    if stats is None:
        stats = IngestStats()
    path = Path(jsonl_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Telemetry file not found: {path}. Run `make data` (or generate_fake_data.py) first."
        )

    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                batch = json.loads(line)
            except json.JSONDecodeError as exc:
                stats.skipped_lines += 1
                if len(stats.errors) < 20:
                    stats.errors.append(f"line {line_no}: bad batch JSON ({exc})")
                continue
            if not isinstance(batch, dict):          # valid JSON but not a batch object
                stats.skipped_lines += 1
                stats.warn(f"line {line_no}: batch is not a JSON object")
                continue

            stats.batches += 1
            for log_event in batch.get("logEvents", []):
                stats.records += 1
                if not isinstance(log_event, dict):
                    stats.skipped_events += 1
                    continue
                message = log_event.get("message")
                try:
                    event = json.loads(message) if isinstance(message, str) else message
                except (json.JSONDecodeError, TypeError) as exc:
                    stats.skipped_events += 1
                    if len(stats.errors) < 20:
                        stats.errors.append(f"line {line_no}: bad event message ({exc})")
                    continue

                row = normalize_event(event) if isinstance(event, dict) else None
                if row is None:
                    stats.skipped_events += 1
                    continue

                stats.events += 1
                stats.note_type(row["event_type"])
                event_id = log_event.get("id")
                yield (str(event_id) if event_id is not None else None), row
