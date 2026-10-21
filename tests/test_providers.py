"""Unit tests for the provider adapters (raw record -> canonical row)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analytics.ingest import IngestStats  # noqa: E402
from analytics.providers import (  # noqa: E402
    CANONICAL_COLUMNS,
    REGISTRY,
    ClaudeCodeAdapter,
    DeepSeekAdapter,
    GeminiAdapter,
    OpenAIAdapter,
    SessionAssigner,
    get_adapters,
    normalize_model,
    validate_providers,
)

KEYS = set(CANONICAL_COLUMNS)


# --- fixtures -----------------------------------------------------------------

def openai_record(**over):
    rec = {
        "id": "chatcmpl-abc", "object": "chat.completion", "created": 1767605400,
        "model": "gpt-4o-2024-08-06", "user": "alex.chen@example.com",
        "client": "chatgpt_enterprise", "conversation_id": "conv_abc", "latency_ms": 1830,
        "usage": {"prompt_tokens": 812, "completion_tokens": 240, "total_tokens": 1052,
                  "prompt_tokens_details": {"cached_tokens": 512},
                  "completion_tokens_details": {"reasoning_tokens": 0}},
        "tool_calls": [{"type": "function", "name": "code_interpreter"}],
        "cost_usd": 0.0031,
        "error": None,
    }
    rec.update(over)
    return rec


def gemini_record(**over):
    rec = {
        "timestamp": "2026-01-05T09:30:00.120Z", "responseId": "r-1",
        "principalEmail": "alex.chen@example.com", "modelVersion": "gemini-2.5-pro",
        "surface": "gemini_code_assist", "sessionId": "g-123", "latencyMs": 2100,
        "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 300,
                          "cachedContentTokenCount": 100, "thoughtsTokenCount": 120,
                          "totalTokenCount": 1320},
        "cost_usd": 0.0052,
        "status": {"code": 200},
    }
    rec.update(over)
    return rec


DS_HEADER = ("request_id,timestamp,user_email,model,prompt_cache_hit_tokens,"
             "prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost_usd,latency_ms,"
             "status_code,error")


def write_deepseek(tmp_path, *lines, header=DS_HEADER):
    data = tmp_path / "deepseek_usage.csv"
    data.write_text(header + "\n" + "\n".join(lines) + "\n")
    return DeepSeekAdapter({"events": data})


# --- normalize_model ------------------------------------------------------------

@pytest.mark.parametrize("provider,raw,family", [
    ("openai", "gpt-4o-2024-08-06", "gpt-4o"),
    ("openai", "gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
    ("openai", "o3-2025-04-16", "o3"),
    ("openai", "gpt-4o", "gpt-4o"),
    ("claude_code", "claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
    ("claude_code", "claude-opus-4-6", "claude-opus-4-6"),
    ("gemini", "models/gemini-2.5-pro", "gemini-2.5-pro"),
    ("gemini", "gemini-2.0-flash-001", "gemini-2.0-flash"),
    ("gemini", "gemini-2.5-pro-preview-05-06", "gemini-2.5-pro"),
    ("gemini", "gemini-exp-1206", "gemini-exp-1206"),
    ("deepseek", " DeepSeek-Chat ", "deepseek-chat"),
    ("openai", None, None),
    ("openai", "", None),
])
def test_normalize_model(provider, raw, family):
    assert normalize_model(provider, raw) == family


# --- registry -------------------------------------------------------------------

def test_registry_and_validation():
    assert list(REGISTRY) == ["claude_code", "openai", "gemini", "deepseek"]
    assert validate_providers(None) == list(REGISTRY)
    assert validate_providers(["gemini", "claude_code"]) == ["claude_code", "gemini"]
    with pytest.raises(ValueError, match="valid"):
        validate_providers(["bard"])
    adapters = get_adapters(["openai"], {"openai": {"events": "/nope.jsonl"}})
    assert len(adapters) == 1 and not adapters[0].available() and not adapters[0].required
    assert ClaudeCodeAdapter().required


# --- OpenAI -----------------------------------------------------------------------

def test_openai_request_maps_tokens_and_tool_calls():
    rows = OpenAIAdapter().normalize(openai_record())
    assert [r["event_type"] for r in rows] == ["api_request", "tool_result"]
    req, tool = rows
    assert set(req) == KEYS and set(tool) == KEYS
    assert req["provider"] == "openai" and req["client"] == "chatgpt_enterprise"
    assert req["model_family"] == "gpt-4o"
    assert req["input_tokens"] == 812 - 512          # uncached prompt
    assert req["cache_read_tokens"] == 512
    assert req["output_tokens"] == 240
    assert req["reasoning_tokens"] == 0
    assert req["cache_creation_tokens"] is None
    assert req["cost_usd"] == pytest.approx(0.0031)     # reported by the source record
    assert req["session_id"] == "conv_abc" and req["source_event_id"] == "chatcmpl-abc"
    assert req["timestamp"] == datetime.fromtimestamp(1767605400, tz=timezone.utc)
    assert req["duration_ms"] == 1830
    assert tool["tool_name"] == "code_interpreter" and tool["success"] is None
    assert tool["session_id"] == "conv_abc" and tool["source_event_id"] == "chatcmpl-abc#tool0"


def test_openai_error_becomes_api_error():
    rows = OpenAIAdapter().normalize(openai_record(
        usage=None, tool_calls=[],
        error={"type": "rate_limit_error", "code": "rate_limit_exceeded", "status": 429}))
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "api_error"
    assert row["status_code"] == "429" and isinstance(row["status_code"], str)
    assert row["error"] == "rate_limit_exceeded"
    assert row["input_tokens"] is None and row["cost_usd"] is None


def test_openai_casts_string_numbers_and_synthesizes_sessions():
    rec = openai_record(user="sam@example.com", conversation_id=None, client="api",
                        tool_calls=None, latency_ms="900", cost_usd="0.25",
                        usage={"prompt_tokens": "100", "completion_tokens": "50"})
    row = OpenAIAdapter().normalize(rec)[0]
    assert row["user_email"] == "sam@example.com" and row["client"] == "api"
    assert row["input_tokens"] == 100 and row["cache_read_tokens"] == 0 and row["output_tokens"] == 50
    assert row["duration_ms"] == 900 and row["cost_usd"] == pytest.approx(0.25)
    assert row["session_id"].startswith("openai:sam@example.com:")   # synthetic session


@pytest.mark.parametrize("bad", ["n/a", -1, None, "nan"])
def test_malformed_cost_becomes_null(bad):
    assert OpenAIAdapter().normalize(openai_record(cost_usd=bad))[0]["cost_usd"] is None


def test_openai_iter_skips_bad_lines(tmp_path):
    f = tmp_path / "openai_usage.jsonl"
    f.write_text("\n".join([
        json.dumps(openai_record()),
        "{not json",
        json.dumps(openai_record(created="not-a-time")),
        json.dumps(["a", "list"]),
    ]) + "\n")
    stats = IngestStats()
    rows = list(OpenAIAdapter({"events": f}).iter_events(stats))
    assert len(rows) == 2                        # request + its tool call
    assert stats.records == 4
    assert stats.skipped_lines == 1 and stats.skipped_events == 2
    assert stats.by_event_type == {"api_request": 1, "tool_result": 1}


# --- Gemini -----------------------------------------------------------------------

def test_gemini_request_maps_tokens():
    row = GeminiAdapter().normalize(gemini_record())
    assert set(row) == KEYS
    assert row["event_type"] == "api_request" and row["provider"] == "gemini"
    assert row["client"] == "gemini_code_assist" and row["session_id"] == "g-123"
    assert row["input_tokens"] == 800 and row["cache_read_tokens"] == 100
    # thoughts are billed as output and are not part of candidatesTokenCount
    assert row["reasoning_tokens"] == 120 and row["output_tokens"] == 300 + 120
    total = row["input_tokens"] + row["cache_read_tokens"] + row["output_tokens"]
    assert total == 1320                          # == totalTokenCount
    assert row["source_event_id"] == "r-1" and row["duration_ms"] == 2100
    assert row["cost_usd"] == pytest.approx(0.0052)


def test_gemini_error_and_optional_fields():
    err = GeminiAdapter().normalize(gemini_record(
        usageMetadata=None, status={"code": 429, "message": "RESOURCE_EXHAUSTED"}))
    assert err["event_type"] == "api_error" and err["status_code"] == "429"
    assert err["error"] == "RESOURCE_EXHAUSTED" and err["cost_usd"] is None

    rec = gemini_record(sessionId=None, responseId=None,
                        usageMetadata={"promptTokenCount": 50, "candidatesTokenCount": 10})
    row = GeminiAdapter().normalize(rec, raw=json.dumps(rec))
    assert row["cache_read_tokens"] == 0 and row["reasoning_tokens"] is None
    assert row["output_tokens"] == 10
    assert row["session_id"].startswith("gemini:alex.chen@example.com:2026-01-05:")
    assert row["source_event_id"].startswith("gemini:sha1:")    # content-hash fallback id


def test_gemini_iter_skips_bad_timestamp(tmp_path):
    f = tmp_path / "gemini_usage.jsonl"
    f.write_text(json.dumps(gemini_record()) + "\n" + json.dumps(gemini_record(timestamp="??")) + "\n")
    stats = IngestStats()
    assert len(list(GeminiAdapter({"events": f}).iter_events(stats))) == 1
    assert stats.skipped_events == 1


# --- DeepSeek -----------------------------------------------------------------------

def test_deepseek_maps_tokens_cost_and_errors(tmp_path):
    adapter = write_deepseek(
        tmp_path,
        "ds-1,2026-01-05T09:30:00.000Z,alex.chen@example.com,deepseek-reasoner,1024,300,800,500,0.0004,7000,200,",
        "ds-2,2026-01-05T09:40:00.000Z,,deepseek-chat,0,100,50,0,0.00005,2000,200,",
        "ds-3,2026-01-05T09:45:00.000Z,alex.chen@example.com,deepseek-chat,,,,,0.0,300,429,Rate limit reached",
        "ds-4,garbage,alex.chen@example.com,deepseek-chat,0,1,1,0,0.1,1,200,",
    )
    stats = IngestStats()
    rows = list(adapter.iter_events(stats))
    assert len(rows) == 3 and stats.skipped_events == 1 and stats.records == 4
    ok, no_email, err = rows
    assert set(ok) == KEYS
    assert ok["user_email"] == "alex.chen@example.com" and ok["client"] == "api"
    assert ok["input_tokens"] == 300 and ok["cache_read_tokens"] == 1024
    assert ok["output_tokens"] == 800 and ok["reasoning_tokens"] == 500
    assert ok["cost_usd"] == pytest.approx(0.0004) and ok["model_family"] == "deepseek-reasoner"
    assert no_email["user_email"] is None
    assert no_email["session_id"].startswith("deepseek:unknown:")
    assert err["event_type"] == "api_error" and err["status_code"] == "429"
    assert err["error"] == "Rate limit reached" and err["input_tokens"] is None and err["cost_usd"] is None
    # same person within 30 min -> same synthetic session
    assert err["session_id"] == ok["session_id"]


def test_deepseek_blank_request_id_gets_content_id(tmp_path):
    line = "  ,2026-01-05T09:30:00.000Z,alex.chen@example.com,deepseek-chat,0,100,50,0,0.0001,2000,200,"
    other = "  ,2026-01-05T09:31:00.000Z,alex.chen@example.com,deepseek-chat,0,100,50,0,0.0001,2000,200,"
    rows = list(write_deepseek(tmp_path, line, line, other).iter_events(IngestStats()))
    ids = [r["source_event_id"] for r in rows]
    assert all(i and i.startswith("deepseek:sha1:") for i in ids)
    assert ids[0] == ids[1] != ids[2]          # exact replay dedups, distinct rows don't


def test_deepseek_bad_header_skips_file(tmp_path):
    adapter = write_deepseek(tmp_path, "x,y", header="foo,bar")
    stats = IngestStats()
    assert list(adapter.iter_events(stats)) == []
    assert any("required columns" in e for e in stats.errors)


# --- Claude Code -----------------------------------------------------------------------

def test_claude_code_adapter_wraps_ingest(tmp_path):
    event = {
        "body": "claude_code.api_request",
        "attributes": {"event.timestamp": "2026-01-15T09:30:00.123Z", "session.id": "s1",
                       "user.email": "alex.chen@example.com", "model": "claude-sonnet-4-5-20250929",
                       "cost_usd": "0.05", "input_tokens": "10", "output_tokens": "5",
                       "cache_read_tokens": "100", "cache_creation_tokens": "7"},
    }
    prompt = {"body": "claude_code.user_prompt",
              "attributes": {"event.timestamp": "2026-01-15T09:29:00.000Z", "session.id": "s1",
                             "prompt_length": "42"}}
    batch = {"logEvents": [{"id": "111", "message": json.dumps(event)},
                           {"id": "222", "message": json.dumps(prompt)}]}
    f = tmp_path / "telemetry_logs.jsonl"
    f.write_text(json.dumps(batch) + "\n")
    stats = IngestStats()
    req, pr = list(ClaudeCodeAdapter({"events": f}).iter_events(stats))
    assert set(req) == KEYS and list(req) == CANONICAL_COLUMNS
    assert req["provider"] == "claude_code" and req["client"] == "claude_code_cli"
    assert req["cost_usd"] == pytest.approx(0.05)
    assert req["model_family"] == "claude-sonnet-4-5" and req["source_event_id"] == "111"
    assert pr["cost_usd"] is None and pr["prompt_length"] == 42
    assert stats.records == 2 and stats.events == 2


def test_claude_code_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(ClaudeCodeAdapter({"events": tmp_path / "nope.jsonl"}).iter_events(IngestStats()))


# --- sessions -----------------------------------------------------------------------

def test_session_assigner_splits_on_30_minute_gap():
    s = SessionAssigner("deepseek")
    t0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    a = s.assign("u", t0)
    b = s.assign("u", t0 + timedelta(minutes=29))
    c = s.assign("u", t0 + timedelta(minutes=58))      # 29 min after b -> same session
    d = s.assign("u", t0 + timedelta(minutes=89))      # 31 min gap -> new session
    other = s.assign("v", t0 + timedelta(minutes=1))
    assert a == b == c == "deepseek:u:2026-01-05:0900"
    assert d == "deepseek:u:2026-01-05:1029"
    assert other.startswith("deepseek:v:")
