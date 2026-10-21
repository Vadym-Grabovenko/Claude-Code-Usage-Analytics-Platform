"""Provider adapter contract + the canonical event schema shared by every adapter.

Each AI provider logs usage in its own raw format. An adapter maps that raw format
to *canonical* event rows; the core (ETL, queries, API, dashboard) only ever sees
canonical rows, so adding a provider never touches the core (ADR-1).

Canonical-row invariants every adapter must uphold:

* the row has exactly the keys of :data:`CANONICAL_SCHEMA` (build it from
  :func:`empty_row`), because the ETL writes rows into a pre-declared table;
* ``provider`` is set, ``timestamp`` is a tz-aware UTC ``datetime``, ``session_id``
  is non-empty (synthesize one with :class:`SessionAssigner` if the source has none);
* token columns follow the mapping in ``data/DATASET.md``: ``input_tokens`` are the
  *uncached* prompt tokens, ``cache_read_tokens`` the cached ones, and
  ``output_tokens`` *all* generated tokens — ``reasoning_tokens`` is the reasoning
  subset of ``output_tokens`` (never added on top), so sums are never double counted;
* ``cost_usd`` comes from the source record (every synthetic format carries it) and is
  set on ``api_request`` rows only; a missing/malformed cost stays NULL (the ETL reports
  such requests as unpriced in ``ingest_runs``).

Standard library only, like ``ingest.py``.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

from ..ingest import IngestStats, _parse_timestamp, _to_float, _to_int  # noqa: F401  (re-exported)

# Ordered (column, DuckDB type). The first 30 columns are the original Claude Code
# schema with the exact types the pre-multi-provider ETL produced (pandas inferred
# DOUBLE for the nullable integer columns) — kept as-is for backward compatibility.
CANONICAL_SCHEMA: list[tuple[str, str]] = [
    ("event_type", "VARCHAR"),
    ("timestamp", "TIMESTAMP WITH TIME ZONE"),
    ("session_id", "VARCHAR"),
    ("user_email", "VARCHAR"),
    ("user_id", "VARCHAR"),
    ("account_uuid", "VARCHAR"),
    ("org_id", "VARCHAR"),
    ("terminal_type", "VARCHAR"),
    ("cli_version", "VARCHAR"),
    ("os_type", "VARCHAR"),
    ("os_version", "VARCHAR"),
    ("host_arch", "VARCHAR"),
    ("resource_practice", "VARCHAR"),
    ("model", "VARCHAR"),
    ("cost_usd", "DOUBLE"),
    ("duration_ms", "DOUBLE"),
    ("input_tokens", "DOUBLE"),
    ("output_tokens", "DOUBLE"),
    ("cache_read_tokens", "DOUBLE"),
    ("cache_creation_tokens", "DOUBLE"),
    ("tool_name", "VARCHAR"),
    ("decision", "VARCHAR"),
    ("decision_source", "VARCHAR"),
    ("decision_type", "VARCHAR"),
    ("success", "BOOLEAN"),
    ("tool_result_size_bytes", "DOUBLE"),
    ("prompt_length", "DOUBLE"),
    ("error", "VARCHAR"),
    ("status_code", "VARCHAR"),
    ("attempt", "DOUBLE"),
    # --- multi-provider extension (all nullable except provider) ---
    ("provider", "VARCHAR NOT NULL"),
    ("client", "VARCHAR"),
    ("model_family", "VARCHAR"),
    ("source_event_id", "VARCHAR"),
    ("reasoning_tokens", "BIGINT"),
]
CANONICAL_COLUMNS: list[str] = [name for name, _ in CANONICAL_SCHEMA]

SESSION_GAP = timedelta(minutes=30)


def empty_row() -> dict[str, Any]:
    """A canonical row with every column present and NULL (insertion-ordered)."""
    return dict.fromkeys(CANONICAL_COLUMNS)


def parse_epoch_seconds(value: Any) -> datetime | None:
    """Epoch seconds (int/float/str) -> tz-aware UTC datetime, or ``None``."""
    seconds = _to_float(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_cost(value: Any) -> float | None:
    """Defensive ``cost_usd`` cast: number/str -> non-negative finite float, else ``None``."""
    cost = _to_float(value)
    if cost is None or cost != cost or cost in (float("inf"), float("-inf")) or cost < 0:
        return None
    return cost


def content_id(provider: str, raw: str) -> str:
    """Deterministic fallback id for sources without one (exact replays dedup)."""
    return f"{provider}:sha1:{hashlib.sha1(raw.encode('utf-8')).hexdigest()}"


class SessionAssigner:
    """Synthesize session ids for sources that don't log one.

    A session is a run of requests by the same principal with gaps of at most
    30 minutes: ``{provider}:{who}:{YYYY-MM-DD}:{HHMM of session start}``.
    Expects (roughly) time-ordered input per principal, which gateway logs are;
    out-of-order input only ever splits sessions, never merges unrelated ones.
    """

    def __init__(self, provider: str, gap: timedelta = SESSION_GAP) -> None:
        self.provider = provider
        self.gap = gap
        self._state: dict[str, tuple[datetime, datetime]] = {}  # who -> (last_ts, start)

    def assign(self, who: str | None, ts: datetime) -> str:
        who = who or "unknown"
        last, start = self._state.get(who, (None, None))
        if last is None or abs(ts - last) > self.gap:
            last, start = ts, ts                 # gap exceeded -> new session
        else:
            last = max(last, ts)
        self._state[who] = (last, start)
        return f"{self.provider}:{who}:{start:%Y-%m-%d}:{start:%H%M}"


@runtime_checkable
class ProviderAdapter(Protocol):
    """What the ETL needs from a provider."""

    name: str          # canonical provider code, e.g. "openai"
    label: str         # human-readable name, e.g. "ChatGPT / OpenAI"
    required: bool     # a missing source file fails the ETL (only Claude Code)

    def default_paths(self) -> dict[str, Path]: ...

    def source_files(self) -> list[Path]: ...

    def available(self) -> bool: ...

    def iter_events(self, stats: IngestStats) -> Iterator[dict[str, Any]]: ...


class BaseAdapter:
    """Shared plumbing: path resolution + availability. Subclasses implement
    ``default_paths`` and ``iter_events``. ``paths["events"]`` is the main source."""

    name: str = ""
    label: str = ""
    required: bool = False

    def __init__(self, paths: dict[str, Path | str] | None = None) -> None:
        merged: dict[str, Path] = dict(self.default_paths())
        for key, value in (paths or {}).items():
            if value is not None:
                merged[key] = Path(value)
        self.paths = merged

    def default_paths(self) -> dict[str, Path]:  # pragma: no cover - abstract
        raise NotImplementedError

    def source_files(self) -> list[Path]:
        return [self.paths["events"]]

    def available(self) -> bool:
        return self.paths["events"].exists()

    def iter_events(self, stats: IngestStats) -> Iterator[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{type(self).__name__}(events={self.paths.get('events')})"
