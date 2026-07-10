"""Tests for the ingest/normalize layer and an end-to-end ETL smoke test."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analytics import etl, queries  # noqa: E402
from analytics.ingest import IngestStats, iter_events, normalize_event  # noqa: E402
from analytics.queries import Filters, detect_anomalies  # noqa: E402


def _api_event():
    return {
        "body": "claude_code.api_request",
        "attributes": {
            "event.timestamp": "2026-01-15T09:30:00.123Z",
            "session.id": "sess-1",
            "user.email": "alex.chen@example.com",
            "user.id": "u1",
            "user.account_uuid": "acct-1",
            "organization.id": "org-1",
            "terminal.type": "vscode",
            "event.name": "api_request",
            "model": "claude-opus-4-6",
            "cost_usd": "0.071",
            "duration_ms": "10230",
            "input_tokens": "263",
            "output_tokens": "454",
            "cache_read_tokens": "73099",
            "cache_creation_tokens": "3149",
        },
        "scope": {"name": "com.anthropic.claude_code.events", "version": "2.1.50"},
        "resource": {"os.type": "darwin", "host.arch": "arm64",
                     "service.version": "2.1.50", "user.practice": "ML Engineering"},
    }


def _batch(*events):
    return {
        "messageType": "DATA_MESSAGE",
        "logGroup": "/claude-code/telemetry",
        "logEvents": [
            {"id": str(i), "timestamp": 1, "message": json.dumps(e)}
            for i, e in enumerate(events)
        ],
        "year": 2026, "month": 1, "day": 15,
    }


def test_normalize_casts_and_flattens():
    row = normalize_event(_api_event())
    assert row is not None
    assert row["event_type"] == "api_request"
    assert row["cost_usd"] == pytest.approx(0.071)          # string -> float
    assert row["input_tokens"] == 263 and isinstance(row["input_tokens"], int)
    assert row["model"] == "claude-opus-4-6"
    assert row["cli_version"] == "2.1.50"
    assert row["timestamp"].year == 2026 and row["timestamp"].hour == 9


def test_normalize_tool_result_success_bool():
    ev = {
        "body": "claude_code.tool_result",
        "attributes": {
            "event.timestamp": "2026-01-15T09:31:00.000Z",
            "session.id": "sess-1", "event.name": "tool_result",
            "tool_name": "Bash", "success": "false", "duration_ms": "5169",
        },
    }
    row = normalize_event(ev)
    assert row["success"] is False
    assert row["tool_name"] == "Bash"


def test_normalize_rejects_missing_required_fields():
    assert normalize_event({"body": "claude_code.api_request", "attributes": {}}) is None


def test_iter_events_unpacks_double_nesting_and_skips_garbage(tmp_path):
    f = tmp_path / "t.jsonl"
    lines = [
        json.dumps(_batch(_api_event(), _api_event())),
        "this is not json",                                  # bad batch line
        json.dumps({"logEvents": [{"message": "{broken"}]}),  # bad event message
    ]
    f.write_text("\n".join(lines) + "\n")

    stats = IngestStats()
    rows = list(iter_events(f, stats))
    assert len(rows) == 2
    assert stats.batches == 2          # two parseable batch lines
    assert stats.skipped_lines == 1    # the "not json" line
    assert stats.skipped_events == 1   # the broken message
    assert stats.by_event_type["api_request"] == 2


def test_iter_events_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(iter_events(tmp_path / "nope.jsonl"))


def test_end_to_end_etl_and_query(tmp_path):
    jsonl = tmp_path / "telemetry_logs.jsonl"
    jsonl.write_text(json.dumps(_batch(_api_event(), _api_event())) + "\n")
    csv = tmp_path / "employees.csv"
    csv.write_text(
        "email,full_name,practice,level,location\n"
        "alex.chen@example.com,Alex Chen,ML Engineering,L5,Germany\n"
    )
    db = tmp_path / "analytics.duckdb"

    stats = etl.build_database(jsonl, csv, db)
    assert stats.events == 2

    import duckdb
    c = duckdb.connect(str(db), read_only=True)
    k = queries.kpis(c, Filters())
    assert int(k["api_requests"]) == 2
    assert k["total_cost"] == pytest.approx(0.142, abs=1e-6)
    # employee join populated the enriched view
    by_practice = queries.cost_by_practice(c, Filters())
    assert "ML Engineering" in set(by_practice["practice"])
    c.close()


def test_detect_anomalies_flags_isolated_spike():
    # a stable series with one obvious spike on day 10
    vals = [10, 11, 9, 10, 12, 10, 9, 11, 10, 10, 40, 10, 11, 9, 10]
    df = detect_anomalies(pd.DataFrame({"v": vals}), "v", window=7, z_thresh=3.0)
    flagged = df.index[df["is_anomaly"]].tolist()
    assert flagged == [10]                       # only the spike
    assert df.loc[10, "z_score"] > 3.0           # judged against prior days, not itself


def test_detect_anomalies_ignores_flat_series():
    df = detect_anomalies(pd.DataFrame({"v": [10] * 15}), "v", window=7, z_thresh=3.0)
    assert df["is_anomaly"].sum() == 0
    # early rows have no baseline yet -> NaN z-score, never flagged
    assert not df["is_anomaly"].iloc[0]
