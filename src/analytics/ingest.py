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

    def note_type(self, event_type: str) -> None:
        self.by_event_type[event_type] = self.by_event_type.get(event_type, 0) + 1


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

            stats.batches += 1
            for log_event in batch.get("logEvents", []):
                message = log_event.get("message")
                try:
                    event = json.loads(message) if isinstance(message, str) else message
                except (json.JSONDecodeError, TypeError) as exc:
                    stats.skipped_events += 1
                    if len(stats.errors) < 20:
                        stats.errors.append(f"line {line_no}: bad event message ({exc})")
                    continue

                row = normalize_event(event or {})
                if row is None:
                    stats.skipped_events += 1
                    continue

                stats.events += 1
                stats.note_type(row["event_type"])
                yield row
