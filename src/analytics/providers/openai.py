"""OpenAI / ChatGPT adapter — ``openai_usage.jsonl`` (one request per line).

Each line is a Chat Completions-style response plus gateway metadata
(``user`` = email, ``client``, ``conversation_id``, ``latency_ms``, ``cost_usd``).
Token mapping: ``input = prompt_tokens - cached_tokens``, ``cache_read = cached_tokens``,
``output = completion_tokens`` (OpenAI already includes reasoning tokens in it),
``reasoning = completion_tokens_details.reasoning_tokens``. ``tool_calls`` become
extra ``tool_result`` rows (``success`` unknown -> NULL). No conversation id (raw API
calls) -> synthetic 30-minute-gap session.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .. import config
from ..ingest import IngestStats
from .base import BaseAdapter, SessionAssigner, _to_int, content_id, empty_row, parse_cost, parse_epoch_seconds
from .models import normalize_model


class OpenAIAdapter(BaseAdapter):
    name = "openai"
    label = "ChatGPT / OpenAI"

    def default_paths(self) -> dict[str, Path]:
        return {"events": config.OPENAI_USAGE_JSONL}

    def iter_events(self, stats: IngestStats) -> Iterator[dict[str, Any]]:
        path = self.paths["events"]
        sessions = SessionAssigner(self.name)
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                stats.records += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    stats.skipped_lines += 1
                    stats.warn(f"{path.name} line {line_no}: bad JSON ({exc})")
                    continue
                rows = self.normalize(rec, sessions, raw=line) if isinstance(rec, dict) else None
                if not rows:
                    stats.skipped_events += 1
                    stats.warn(f"{path.name} line {line_no}: missing/invalid 'created' timestamp")
                    continue
                for row in rows:
                    stats.events += 1
                    stats.note_type(row["event_type"])
                    yield row

    def normalize(
        self, rec: dict[str, Any], sessions: SessionAssigner | None = None, raw: str | None = None
    ) -> list[dict[str, Any]] | None:
        """One raw record -> canonical rows (request/error row + tool rows), or None."""
        ts = parse_epoch_seconds(rec.get("created"))
        if ts is None:
            return None
        sessions = sessions or SessionAssigner(self.name)
        email = str(rec.get("user") or "").strip() or None
        source_id = rec.get("id") or (content_id(self.name, raw) if raw else None)

        row = empty_row()
        row.update(
            provider=self.name,
            timestamp=ts,
            user_email=email,
            client=rec.get("client") or "api",
            model=rec.get("model"),
            model_family=normalize_model(self.name, rec.get("model")),
            duration_ms=_to_int(rec.get("latency_ms")),
            source_event_id=source_id,
        )
        row["session_id"] = rec.get("conversation_id") or sessions.assign(email, ts)

        error = rec.get("error")
        if error:
            err = error if isinstance(error, dict) else {"message": str(error)}
            status = _to_int(err.get("status") or err.get("status_code"))
            row["event_type"] = "api_error"
            row["status_code"] = str(status) if status is not None else None
            row["error"] = err.get("code") or err.get("type") or err.get("message")
            return [row]

        row["event_type"] = "api_request"
        row["cost_usd"] = parse_cost(rec.get("cost_usd"))
        usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else None
        if usage:
            prompt = _to_int(usage.get("prompt_tokens"))
            cached = _to_int((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
            row["input_tokens"] = max(prompt - cached, 0) if prompt is not None else None
            row["cache_read_tokens"] = cached if prompt is not None else None
            row["output_tokens"] = _to_int(usage.get("completion_tokens"))
            row["reasoning_tokens"] = _to_int(
                (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
        rows = [row]

        for i, call in enumerate(rec.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            tool = call.get("name") or (call.get("function") or {}).get("name") or call.get("type")
            trow = empty_row()
            trow.update(
                provider=self.name, event_type="tool_result", timestamp=ts,
                session_id=row["session_id"], user_email=email, client=row["client"],
                tool_name=tool, source_event_id=f"{source_id}#tool{i}" if source_id else None,
            )
            rows.append(trow)
        return rows
