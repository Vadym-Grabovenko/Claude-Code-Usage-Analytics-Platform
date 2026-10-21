"""API tests — build a tiny DuckDB, point config at it, exercise the endpoints."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# api lives at the repo root; src holds the analytics package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from analytics import config, etl  # noqa: E402
from api.main import app  # noqa: E402


def _api_event(email="alex.chen@example.com", cost="0.071"):
    return {
        "body": "claude_code.api_request",
        "attributes": {
            "event.timestamp": "2026-01-15T09:30:00.123Z",
            "session.id": "sess-1", "user.email": email, "user.id": "u1",
            "terminal.type": "vscode", "model": "claude-opus-4-6",
            "cost_usd": cost, "duration_ms": "10230", "input_tokens": "263",
            "output_tokens": "454", "cache_read_tokens": "73099",
            "cache_creation_tokens": "3149",
        },
        "resource": {"os.type": "darwin", "service.version": "2.1.50"},
    }


def _batch(*events):
    return {
        "messageType": "DATA_MESSAGE", "logGroup": "/claude-code/telemetry",
        "logEvents": [{"id": str(i), "timestamp": 1, "message": json.dumps(e)}
                      for i, e in enumerate(events)],
        "year": 2026, "month": 1, "day": 15,
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    jsonl = tmp_path / "telemetry_logs.jsonl"
    jsonl.write_text(json.dumps(_batch(_api_event(), _api_event())) + "\n")
    csv = tmp_path / "employees.csv"
    csv.write_text("email,full_name,practice,level,location\n"
                   "alex.chen@example.com,Alex Chen,ML Engineering,L5,Germany\n")
    db = tmp_path / "analytics.duckdb"
    etl.build_database(jsonl, csv, db)
    # queries.connect() reads config.DUCKDB_PATH at call time -> patch it to our DB
    monkeypatch.setattr(config, "DUCKDB_PATH", db)
    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_kpis(client):
    r = client.get("/kpis")
    assert r.status_code == 200
    body = r.json()
    assert int(body["api_requests"]) == 2
    assert body["total_cost"] == pytest.approx(0.142, abs=1e-6)


def test_cost_by_model(client):
    r = client.get("/cost/by-model")
    assert r.status_code == 200
    assert any(row["model"] == "claude-opus-4-6" for row in r.json())


def test_filters_applied(client):
    # a practice filter that matches -> data; one that doesn't -> empty
    assert client.get("/cost/by-practice", params={"practices": "ML Engineering"}).json()
    assert client.get("/cost/by-practice", params={"practices": "Nonexistent"}).json() == []


def test_adoption_bad_dimension_returns_400(client):
    r = client.get("/adoption/drop_table")
    assert r.status_code == 400


def test_anomalies_endpoint_shape(client):
    r = client.get("/anomalies/cost", params={"window": 7, "z_thresh": 3.0})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# --- Phase 3: multi-provider endpoints ---------------------------------------
# A tiny 4-provider dataset. OpenAI has two rows for family gpt-4o: one with the raw id
# "gpt-4o" and one dated snapshot, so `models` (raw, exact) and `model_families` differ.

def _openai_row(id_, model, cost, user="alex.chen@example.com", error=None):
    return {
        "id": id_, "object": "chat.completion", "created": 1768478400,  # 2026-01-15T12:00Z
        "model": model, "user": user, "client": "chatgpt_enterprise",
        "conversation_id": f"conv-{id_}", "latency_ms": 1500,
        "usage": None if error else {
            "prompt_tokens": 1000, "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 200},
            "completion_tokens_details": {"reasoning_tokens": 0}},
        "tool_calls": [], "cost_usd": 0.0 if error else cost, "error": error,
    }


@pytest.fixture()
def multi_client(tmp_path, monkeypatch):
    (tmp_path / "telemetry_logs.jsonl").write_text(
        json.dumps(_batch(_api_event(), _api_event())) + "\n")
    (tmp_path / "employees.csv").write_text(
        "email,full_name,practice,level,location\n"
        "alex.chen@example.com,Alex Chen,ML Engineering,L5,Germany\n"
        "sam.lee@example.com,Sam Lee,Backend Engineering,L3,Poland\n")
    (tmp_path / "openai_usage.jsonl").write_text("\n".join(json.dumps(r) for r in [
        _openai_row("chatcmpl-1", "gpt-4o", 0.01),
        _openai_row("chatcmpl-2", "gpt-4o-2024-08-06", 0.02),
        _openai_row("chatcmpl-3", "gpt-4o-2024-08-06", 0.0, user="sam.lee@example.com",
                    error={"type": "rate_limit_error", "code": "rate_limit_exceeded",
                           "status": 429}),
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
        "ds-1,2026-01-15T12:00:00.000Z,sam.lee@example.com,deepseek-chat,500,500,100,0,0.0004,"
        "2000,200,\n")
    db = tmp_path / "analytics.duckdb"
    etl.build_database(tmp_path / "telemetry_logs.jsonl", tmp_path / "employees.csv", db,
                       providers=["claude_code", "openai", "gemini", "deepseek"])
    monkeypatch.setattr(config, "DUCKDB_PATH", db)
    return TestClient(app)


ALL_PROVIDERS = {"claude_code", "openai", "gemini", "deepseek"}


def test_openapi_metadata(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["version"] == "1.1.0"
    assert spec["info"]["title"] == "AI Usage Analytics API"


def test_providers_list(multi_client):
    r = multi_client.get("/providers")
    assert r.status_code == 200
    body = {row["provider"]: row for row in r.json()}
    assert set(body) == ALL_PROVIDERS
    assert all(row["has_data"] and row["events"] > 0 for row in body.values())
    assert body["openai"]["label"]


def test_providers_list_marks_missing_provider(client):
    # the legacy fixture only has Claude Code data
    body = {row["provider"]: row for row in client.get("/providers").json()}
    assert body["claude_code"]["has_data"] is True
    assert body["openai"] == {**body["openai"], "has_data": False, "events": 0}


def test_providers_summary(multi_client):
    r = multi_client.get("/providers/summary")
    assert r.status_code == 200
    rows = {row["provider"]: row for row in r.json()}
    assert set(rows) == ALL_PROVIDERS
    assert rows["openai"]["requests"] == 2 and rows["openai"]["errors"] == 1
    assert rows["openai"]["error_rate"] == pytest.approx(0.5)
    assert rows["openai"]["total_cost"] == pytest.approx(0.03)
    assert rows["claude_code"]["total_cost"] == pytest.approx(0.142)


def test_providers_filter_applies(multi_client):
    rows = multi_client.get("/providers/summary", params={"providers": ["gemini", "deepseek"]}).json()
    assert {row["provider"] for row in rows} == {"gemini", "deepseek"}
    kpis = multi_client.get("/kpis", params={"providers": "claude_code"}).json()
    assert int(kpis["api_requests"]) == 2
    assert kpis["total_cost"] == pytest.approx(0.142, abs=1e-6)
    # no provider filter -> all providers
    assert int(multi_client.get("/kpis").json()["providers_in_use"]) == 4


def test_invalid_provider_returns_422_with_allowed_values(multi_client):
    r = multi_client.get("/kpis", params={"providers": ["openai", "bogus"]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, list) and len(detail) == 1      # FastAPI's standard shape
    err = detail[0]
    assert err["loc"] == ["query", "providers"] and err["input"] == "bogus"
    assert err["type"] and err["msg"]
    assert set(err["ctx"]["expected"]) == ALL_PROVIDERS
    # same envelope as a native FastAPI validation error (e.g. a bad grain)
    native = multi_client.get("/providers/share", params={"grain": "year"}).json()["detail"][0]
    assert set(native) >= {"type", "loc", "msg", "input"} and set(err) >= set(native) - {"url"}
    # applies to every filtered endpoint, including the new ones
    assert multi_client.get("/providers/summary", params={"providers": "x"}).status_code == 422


def test_models_filter_is_raw_id_only(multi_client):
    rows = multi_client.get("/cost/by-model", params={"models": "gpt-4o"}).json()
    assert [r["model"] for r in rows] == ["gpt-4o"]  # the dated snapshot is NOT included
    assert rows[0]["requests"] == 1


def test_model_families_filter_matches_family(multi_client):
    rows = multi_client.get("/cost/by-model", params={"model_families": "gpt-4o"}).json()
    assert {r["model"] for r in rows} == {"gpt-4o", "gpt-4o-2024-08-06"}
    assert sum(r["requests"] for r in rows) == 2
    # combined with a raw id the two filters intersect
    rows = multi_client.get("/cost/by-model", params={
        "model_families": "gpt-4o", "models": "gpt-4o-2024-08-06"}).json()
    assert [r["model"] for r in rows] == ["gpt-4o-2024-08-06"]


def test_filters_models_dashboard_compat():
    """Filters.models keeps matching model OR family by default (dashboard sends families)."""
    from analytics.queries import Filters
    where, params = Filters(models=["gpt-4o"]).clause()
    assert "model_family IN" in where and params == ["gpt-4o", "gpt-4o"]
    where, params = Filters(models=["gpt-4o"], models_match_family=False).clause()
    assert where == "model IN (?)" and params == ["gpt-4o"]
    where, params = Filters(model_families=["gpt-4o"]).clause()
    assert where == "model_family IN (?)" and params == ["gpt-4o"]


@pytest.mark.parametrize("grain", ["day", "week", "month"])
def test_providers_share(multi_client, grain):
    r = multi_client.get("/providers/share", params={"grain": grain})
    assert r.status_code == 200
    rows = r.json()
    assert {row["provider"] for row in rows} == ALL_PROVIDERS
    assert sum(row["cost_share"] for row in rows) == pytest.approx(1.0)


def test_providers_share_bad_grain_returns_422(multi_client):
    assert multi_client.get("/providers/share", params={"grain": "year"}).status_code == 422


def test_providers_by_dimension(multi_client):
    r = multi_client.get("/providers/by/practice")
    assert r.status_code == 200
    rows = r.json()
    assert {(row["practice"], row["provider"]) for row in rows} >= {
        ("ML Engineering", "claude_code"), ("Backend Engineering", "gemini")}


def test_providers_by_bad_dimension_returns_400(multi_client):
    assert multi_client.get("/providers/by/drop_table").status_code == 400


def test_providers_multi_users(multi_client):
    r = multi_client.get("/providers/multi-users")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"distribution", "pairs"}
    dist = {row["n_providers"]: row["users"] for row in body["distribution"]}
    # alex: claude_code + openai; sam: openai (error) + gemini + deepseek
    assert dist == {2: 1, 3: 1}
    pairs = {(p["provider_a"], p["provider_b"]): p["users"] for p in body["pairs"]}
    assert pairs[("claude_code", "openai")] == 1


def test_models_leaderboard(multi_client):
    rows = multi_client.get("/models/leaderboard").json()
    families = {(r["provider"], r["model_family"]) for r in rows}
    assert ("openai", "gpt-4o") in families and ("deepseek", "deepseek-chat") in families
    gpt = next(r for r in rows if r["model_family"] == "gpt-4o")
    assert gpt["requests"] == 2 and gpt["errors"] == 1
    assert len(multi_client.get("/models/leaderboard", params={"limit": 2}).json()) == 2
    assert multi_client.get("/models/leaderboard", params={"limit": 0}).status_code == 422


def test_data_quality(multi_client):
    r = multi_client.get("/data-quality")
    assert r.status_code == 200
    rows = {row["provider"]: row for row in r.json()}
    assert set(rows) == ALL_PROVIDERS
    assert rows["openai"]["rows_loaded"] == 3


@pytest.mark.parametrize("path", ["/anomalies/cost", "/anomalies/errors", "/errors/rate"])
def test_by_provider_param(multi_client, path):
    flat = multi_client.get(path)
    split = multi_client.get(path, params={"by_provider": True})
    assert flat.status_code == split.status_code == 200
    assert flat.json() and "provider" not in flat.json()[0]
    assert {row["provider"] for row in split.json()} == ALL_PROVIDERS
