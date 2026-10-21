"""Multi-provider ETL + query-layer tests on a tiny hand-built 4-provider dataset."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analytics import etl, queries  # noqa: E402
from analytics.queries import Filters  # noqa: E402

# Midday UTC so the local-timezone DATE cast in the view can't shift the day.
JAN15 = int(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).timestamp())


def _cc_event(cost, minute):
    return {
        "body": "claude_code.api_request",
        "attributes": {
            "event.timestamp": f"2026-01-15T12:{minute:02d}:00.000Z", "session.id": "sess-1",
            "user.email": "alex.chen@example.com", "model": "claude-opus-4-6", "cost_usd": cost,
            "duration_ms": "10000", "input_tokens": "100", "output_tokens": "50",
            "cache_read_tokens": "1000", "cache_creation_tokens": "10",
        },
        "resource": {"os.type": "darwin", "service.version": "2.1.50"},
    }


def _openai(id_, model="gpt-4o-2024-08-06", cost=0.002, error=None, user="alex.chen@example.com"):
    return {
        "id": id_, "object": "chat.completion", "created": JAN15, "model": model,
        "user": user, "client": "chatgpt_enterprise", "conversation_id": f"conv-{id_}",
        "latency_ms": 1500,
        "usage": None if error else {
            "prompt_tokens": 1000, "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 200},
            "completion_tokens_details": {"reasoning_tokens": 0}},
        "tool_calls": [], "cost_usd": 0.0 if error else cost, "error": error,
    }


@pytest.fixture()
def dataset(tmp_path):
    batch = {"logEvents": [{"id": "1", "message": json.dumps(_cc_event("0.071", 30))},
                           {"id": "2", "message": json.dumps(_cc_event("0.029", 31))}]}
    (tmp_path / "telemetry_logs.jsonl").write_text(json.dumps(batch) + "\n")
    (tmp_path / "employees.csv").write_text(
        "email,full_name,practice,level,location\n"
        "alex.chen@example.com,Alex Chen,ML Engineering,L5,Germany\n"
        "sam.lee@example.com,Sam Lee,Backend Engineering,L3,Poland\n")
    dup = json.dumps(_openai("chatcmpl-1"))
    (tmp_path / "openai_usage.jsonl").write_text("\n".join([
        json.dumps(_openai("chatcmpl-0", model="o3-2025-04-16", cost=0.01)),
        dup, dup,                                                    # replayed line -> dedup
        json.dumps(_openai("chatcmpl-2", cost="n/a")),               # malformed cost -> NULL
        json.dumps(_openai("chatcmpl-3", user="sam.lee@example.com",
                           error={"type": "rate_limit_error", "code": "rate_limit_exceeded",
                                  "status": 429})),
        '{"id": "truncated',                                         # bad line -> skipped
    ]) + "\n")
    (tmp_path / "gemini_usage.jsonl").write_text(json.dumps({
        "timestamp": "2026-01-15T12:00:00.000Z", "responseId": "g1",
        "principalEmail": "sam.lee@example.com", "modelVersion": "gemini-2.5-pro",
        "surface": "gemini_app", "sessionId": "gs1", "latencyMs": 2000,
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100,
                          "thoughtsTokenCount": 100},
        "cost_usd": 0.003, "status": {"code": 200}}) + "\n")
    (tmp_path / "deepseek_usage.csv").write_text(
        "request_id,timestamp,user_email,model,prompt_cache_hit_tokens,prompt_cache_miss_tokens,"
        "completion_tokens,reasoning_tokens,cost_usd,latency_ms,status_code,error\n"
        "ds-1,2026-01-15T12:00:00.000Z,sam.lee@example.com,deepseek-chat,500,500,100,0,0.0004,2000,200,\n"
        "ds-2,2026-01-15T12:05:00.000Z,sam.lee@example.com,deepseek-chat,0,100,10,0,0.0001,2000,200,\n"
        ",2026-01-15T12:09:00.000Z,sam.lee@example.com,deepseek-chat,0,10,1,0,0.00001,900,200,\n"
        ",2026-01-15T12:09:00.000Z,sam.lee@example.com,deepseek-chat,0,10,1,0,0.00001,900,200,\n")
    return tmp_path


def _build(d: Path, **kw):
    db = d / "analytics.duckdb"
    stats = etl.build_database(d / "telemetry_logs.jsonl", d / "employees.csv", db, **kw)
    return duckdb.connect(str(db), read_only=True), stats


def test_all_providers_loaded_with_dedup_and_reported_costs(dataset):
    con, stats = _build(dataset)
    by_provider = dict(con.execute("SELECT provider, count(*) FROM events GROUP BY 1").fetchall())
    assert by_provider == {"claude_code": 2, "openai": 4, "gemini": 1, "deepseek": 3}
    assert con.execute("SELECT count(*) FROM events WHERE provider IS NULL").fetchone()[0] == 0
    assert stats.duplicates == 2 and stats.skipped_lines == 1     # 1 OpenAI replay + 1 id-less DeepSeek replay

    cost = dict(con.execute("SELECT source_event_id, cost_usd FROM events WHERE provider='openai'").fetchall())
    assert cost["chatcmpl-0"] == pytest.approx(0.01)
    assert cost["chatcmpl-1"] == pytest.approx(0.002)
    assert cost["chatcmpl-2"] is None                     # malformed cost -> NULL, no crash
    assert cost["chatcmpl-3"] is None                     # errors carry no cost
    totals = dict(con.execute("SELECT provider, sum(cost_usd) FROM events GROUP BY 1").fetchall())
    assert totals["claude_code"] == pytest.approx(0.1)
    assert totals["gemini"] == pytest.approx(0.003) and totals["deepseek"] == pytest.approx(0.00051)

    gem = con.execute("SELECT output_tokens, reasoning_tokens FROM events WHERE provider='gemini'").fetchone()
    assert gem == (200, 100)                              # thoughts folded into output
    assert "openai:gpt-4o" in stats.unknown_models

    dq = queries.data_quality(con).set_index("provider")
    assert dq.loc["openai", "unknown_models"] == "gpt-4o"
    assert dq.loc["openai", "duplicates_removed"] == 1 and dq.loc["openai", "rows_read"] == 6
    assert dq.loc["openai", "rows_skipped"] == 1 and dq.loc["openai", "unpriced_requests"] == 1
    assert dq.loc["claude_code", "status"] == "ok"
    for col in ["source_file", "rows_loaded", "run_at"]:
        assert col in dq.columns
    con.close()


def test_schema_has_no_pricing_or_key_artifacts(dataset):
    con, _ = _build(dataset)
    cols = {r[0] for r in con.execute("DESCRIBE events").fetchall()}
    assert {"provider", "client", "model_family", "source_event_id", "reasoning_tokens"} <= cols
    assert not cols & {"cost_source", "api_key_id"}
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert {"events", "employees", "ingest_runs", "provider_labels"} <= tables
    assert not tables & {"pricing", "api_keys"}
    con.close()


def test_total_tokens_coalesces_null_terms(dataset):
    con, _ = _build(dataset)
    # OpenAI never has cache_creation_tokens; the old view returned NULL for these rows.
    tt = con.execute("SELECT total_tokens FROM events_enriched WHERE source_event_id='chatcmpl-0'").fetchone()[0]
    assert tt == 800 + 200 + 100
    con.close()


def test_provider_filter_and_backward_compatible_kpis(dataset):
    con, _ = _build(dataset)
    cc = queries.kpis(con, Filters(providers=["claude_code"]))
    assert int(cc["api_requests"]) == 2 and cc["total_cost"] == pytest.approx(0.1)
    assert int(cc["total_tokens"]) == 2 * (100 + 50 + 1000 + 10)
    everything = queries.kpis(con, Filters())
    assert int(everything["api_requests"]) == 2 + 3 + 1 + 3
    assert int(everything["providers_in_use"]) == 4
    assert everything["total_cost"] == pytest.approx(
        sum(queries.kpis(con, Filters(providers=[p]))["total_cost"]
            for p in ["claude_code", "openai", "gemini", "deepseek"]))
    # `models` = raw ids (exact), `model_families` = normalized families
    assert int(queries.kpis(con, Filters(models=["gpt-4o-2024-08-06"]))["api_requests"]) == 2
    assert int(queries.kpis(con, Filters(models=["gpt-4o"]))["api_requests"]) == 0
    assert int(queries.kpis(con, Filters(model_families=["gpt-4o"]))["api_requests"]) == 2
    opts = queries.filter_options(con)
    assert opts["providers"] == ["claude_code", "deepseek", "gemini", "openai"]
    assert opts["models_by_provider"]["openai"] == ["gpt-4o", "o3"]
    con.close()


def test_provider_queries_shapes(dataset):
    con, _ = _build(dataset)
    f = Filters()
    summary = queries.provider_summary(con, f).set_index("provider")
    for col in ["users", "total_cost", "cost_per_user", "error_rate", "cost_per_1k_tokens",
                "p50_latency_ms", "p95_latency_ms"]:
        assert col in summary.columns
    assert "estimated_cost_share" not in summary.columns
    assert summary.loc["openai", "error_rate"] == pytest.approx(1 / 3)
    # cost per 1K tokens ignores the request with no cost
    assert summary.loc["openai", "cost_per_1k_tokens"] == pytest.approx((0.01 + 0.002) * 1000 / 2200)

    share = queries.provider_share_over_time(con, f, grain="day")
    assert {"period", "provider", "total_cost", "requests", "users", "cost_share"} <= set(share.columns)
    assert share["cost_share"].sum() == pytest.approx(1.0)
    assert not queries.provider_share_over_time(con, f).empty          # default grain=week
    with pytest.raises(ValueError):
        queries.provider_share_over_time(con, f, grain="hour")

    by_practice = queries.provider_by_dimension(con, f, "practice")
    assert {"practice", "provider", "users", "total_cost"} <= set(by_practice.columns)
    assert {"level", "provider", "users", "total_cost"} <= set(queries.provider_by_dimension(con, f, "level").columns)
    with pytest.raises(ValueError):
        queries.provider_by_dimension(con, f, "user_email; DROP TABLE events")

    multi = queries.multi_provider_users(con, f)
    dist = multi["distribution"].set_index("n_providers")["users"].to_dict()
    assert dist == {2: 1, 3: 1}       # alex: claude_code+openai; sam: openai+gemini+deepseek
    assert {"provider_a", "provider_b", "users"} <= set(multi["pairs"].columns)
    assert len(multi["pairs"]) == 4

    board = queries.model_leaderboard(con, f)
    assert {"provider", "model_family", "total_cost", "avg_cost", "requests"} <= set(board.columns)
    assert board.iloc[0]["model_family"] == "claude-opus-4-6"          # sorted by cost

    anomalies = queries.cost_anomalies(con, f, by_provider=True)
    assert "provider" in anomalies.columns and {"baseline", "z_score", "is_anomaly"} <= set(anomalies.columns)
    assert "provider" in queries.error_rate_anomalies(con, f, by_provider=True).columns
    assert "provider" not in queries.cost_anomalies(con, f).columns
    assert "provider" in queries.error_rate_over_time(con, f, by_provider=True).columns
    assert not queries.adoption_by_dimension(con, f, "client").empty
    assert not hasattr(queries, "unattributed_usage")
    con.close()


def test_missing_optional_provider_file_is_a_warning(dataset):
    (dataset / "gemini_usage.jsonl").unlink()
    (dataset / "deepseek_usage.csv").unlink()
    con, stats = _build(dataset)
    assert set(stats.by_provider) == {"claude_code", "openai"}
    status = dict(con.execute("SELECT provider, status FROM ingest_runs").fetchall())
    assert status == {"claude_code": "ok", "openai": "ok", "gemini": "missing", "deepseek": "missing"}
    con.close()


def test_provider_subset_and_missing_claude_code_still_fails(dataset):
    con, stats = _build(dataset, providers=["claude_code", "gemini"])
    assert set(stats.by_provider) == {"claude_code", "gemini"}
    con.close()
    (dataset / "telemetry_logs.jsonl").unlink()
    with pytest.raises(FileNotFoundError):
        _build(dataset)
