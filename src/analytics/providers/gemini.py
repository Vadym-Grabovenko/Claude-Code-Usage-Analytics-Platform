"""Gemini adapter — ``gemini_usage.jsonl`` (Vertex AI / Gemini API request log).

camelCase ``usageMetadata``. Token mapping: ``input = promptTokenCount -
cachedContentTokenCount``, ``cache_read = cachedContentTokenCount``,
``reasoning = thoughtsTokenCount``, and ``output = candidatesTokenCount +
thoughtsTokenCount``. Gemini reports thoughts *outside* ``candidatesTokenCount``
(``totalTokenCount = prompt + candidates + thoughts``) but bills them as output, so
they are folded into ``output_tokens`` to keep the canonical invariant
"reasoning ⊆ output" — identical semantics to OpenAI/DeepSeek and correct totals/cost.
A non-200 ``status.code`` is an ``api_error``. ``cost_usd`` is taken from the record.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .. import config
from ..ingest import IngestStats
from .base import BaseAdapter, SessionAssigner, _parse_timestamp, _to_int, content_id, empty_row, parse_cost
from .models import normalize_model


class GeminiAdapter(BaseAdapter):
    name = "gemini"
    label = "Gemini"

    def default_paths(self) -> dict[str, Path]:
        return {"events": config.GEMINI_USAGE_JSONL}

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
                row = self.normalize(rec, sessions, raw=line) if isinstance(rec, dict) else None
                if row is None:
                    stats.skipped_events += 1
                    stats.warn(f"{path.name} line {line_no}: missing/invalid timestamp")
                    continue
                stats.events += 1
                stats.note_type(row["event_type"])
                yield row

    def normalize(
        self, rec: dict[str, Any], sessions: SessionAssigner | None = None, raw: str | None = None
    ) -> dict[str, Any] | None:
        ts = _parse_timestamp(rec.get("timestamp"))
        if ts is None:
            return None
        sessions = sessions or SessionAssigner(self.name)
        email = (rec.get("principalEmail") or "").strip() or None
        model = rec.get("modelVersion") or rec.get("model")

        row = empty_row()
        row.update(
            provider=self.name,
            timestamp=ts,
            user_email=email,
            client=rec.get("surface") or "api",
            model=model,
            model_family=normalize_model(self.name, model),
            duration_ms=_to_int(rec.get("latencyMs")),
            source_event_id=rec.get("responseId") or (content_id(self.name, raw) if raw else None),
        )
        row["session_id"] = rec.get("sessionId") or sessions.assign(email, ts)

        status = rec.get("status") if isinstance(rec.get("status"), dict) else {}
        code = _to_int(status.get("code"))
        usage = rec.get("usageMetadata") if isinstance(rec.get("usageMetadata"), dict) else None
        if (code is not None and code != 200) or (code is None and usage is None and status):
            row["event_type"] = "api_error"
            row["status_code"] = str(code) if code is not None else None
            row["error"] = status.get("message") or status.get("status")
            return row

        row["event_type"] = "api_request"
        row["cost_usd"] = parse_cost(rec.get("cost_usd", rec.get("costUsd")))
        if usage:
            prompt = _to_int(usage.get("promptTokenCount"))
            cached = _to_int(usage.get("cachedContentTokenCount")) or 0
            candidates = _to_int(usage.get("candidatesTokenCount"))
            thoughts = _to_int(usage.get("thoughtsTokenCount"))
            row["input_tokens"] = max(prompt - cached, 0) if prompt is not None else None
            row["cache_read_tokens"] = cached if prompt is not None else None
            if candidates is not None or thoughts is not None:
                row["output_tokens"] = (candidates or 0) + (thoughts or 0)
            row["reasoning_tokens"] = thoughts
        return row
