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
