"""Claude Code adapter — wraps the existing doubly-nested JSONL parser.

``ingest.iter_events`` / ``normalize_event`` stay the source of truth for parsing;
this adapter only adds the multi-provider columns (``cost_usd`` is reported as-is).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from .. import config
from ..ingest import IngestStats, iter_log_events
from .base import BaseAdapter, empty_row
from .models import normalize_model


class ClaudeCodeAdapter(BaseAdapter):
    name = "claude_code"
    label = "Claude Code"
    required = True          # the original dataset: a missing file still fails the ETL

    def default_paths(self) -> dict[str, Path]:
        return {"events": config.TELEMETRY_JSONL}

    def iter_events(self, stats: IngestStats) -> Iterator[dict[str, Any]]:
        # iter_log_events raises FileNotFoundError for a missing file (required source).
        for event_id, parsed in iter_log_events(self.paths["events"], stats):
            row = empty_row()
            row.update(parsed)
            row["provider"] = self.name
            row["client"] = "claude_code_cli"
            row["model_family"] = normalize_model(self.name, row["model"])
            row["source_event_id"] = event_id
            yield row
