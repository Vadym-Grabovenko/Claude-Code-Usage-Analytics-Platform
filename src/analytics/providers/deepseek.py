"""DeepSeek adapter — ``deepseek_usage.csv`` written by the API gateway.

Columns: ``request_id, timestamp, user_email, model, prompt_cache_hit_tokens,
prompt_cache_miss_tokens, completion_tokens, reasoning_tokens, cost_usd, latency_ms,
status_code, error``. Token mapping: ``input = prompt_cache_miss_tokens``,
``cache_read = prompt_cache_hit_tokens``, ``output = completion_tokens`` (includes
reasoning, as in the DeepSeek API), ``reasoning = reasoning_tokens``. No session id in
the source -> synthetic 30-minute-gap sessions.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterator

from .. import config
from ..ingest import IngestStats
from .base import BaseAdapter, SessionAssigner, _parse_timestamp, _to_int, content_id, empty_row, parse_cost
from .models import normalize_model

REQUIRED_COLUMNS = ("timestamp", "user_email", "model")


def _raw(rec: dict[str, Any]) -> str:
    """Canonical text of a CSV row (column order independent) for the fallback id."""
    return "\x1f".join(f"{k}={'' if v is None else v}" for k, v in sorted(rec.items(), key=lambda kv: str(kv[0])))


class DeepSeekAdapter(BaseAdapter):
    name = "deepseek"
    label = "DeepSeek"

    def default_paths(self) -> dict[str, Path]:
        return {"events": config.DEEPSEEK_USAGE_CSV}

    def iter_events(self, stats: IngestStats) -> Iterator[dict[str, Any]]:
        path = self.paths["events"]
        sessions = SessionAssigner(self.name)
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                stats.warn(f"{path.name}: header lacks required columns {missing}; file skipped")
                return
            for rec in reader:
                stats.records += 1
                row = self.normalize(rec, sessions)
                if row is None:
                    stats.skipped_events += 1
                    stats.warn(f"{path.name} line {reader.line_num}: missing/invalid timestamp")
                    continue
                stats.events += 1
                stats.note_type(row["event_type"])
                yield row

    def normalize(self, rec: dict[str, Any], sessions: SessionAssigner | None = None) -> dict[str, Any] | None:
        ts = _parse_timestamp((rec.get("timestamp") or "").strip() or None)
        if ts is None:
            return None
        sessions = sessions or SessionAssigner(self.name)
        email = (rec.get("user_email") or "").strip() or None
        model = (rec.get("model") or "").strip() or None

        row = empty_row()
        row.update(
            provider=self.name,
            timestamp=ts,
            user_email=email,
            client="api",
            model=model,
            model_family=normalize_model(self.name, model),
            duration_ms=_to_int(rec.get("latency_ms")),
            source_event_id=(rec.get("request_id") or "").strip() or content_id(self.name, _raw(rec)),
            session_id=sessions.assign(email, ts),
        )

        status = _to_int(rec.get("status_code"))
        error = (rec.get("error") or "").strip() or None
        if (status is not None and status != 200) or error:
            row["event_type"] = "api_error"
            row["status_code"] = str(status) if status is not None else None
            row["error"] = error
            return row

        row["event_type"] = "api_request"
        row["cost_usd"] = parse_cost(rec.get("cost_usd"))
        row["input_tokens"] = _to_int(rec.get("prompt_cache_miss_tokens"))
        row["cache_read_tokens"] = _to_int(rec.get("prompt_cache_hit_tokens"))
        row["output_tokens"] = _to_int(rec.get("completion_tokens"))
        row["reasoning_tokens"] = _to_int(rec.get("reasoning_tokens"))
        return row
