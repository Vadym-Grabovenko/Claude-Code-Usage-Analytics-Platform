# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

This is a **test task** solution, not an existing product: a **Claude Code Usage Analytics Platform** — an end-to-end system that ingests synthetic Claude Code telemetry, extracts usage/behavioral insights, and presents them in interactive dashboards. The pipeline, dashboard, API, tests, and agent setup are built and live in this repo; the only non-authored file is the provided dataset generator (`data/generate_fake_data.py`).

## Hard requirements from the test task (design to these)

These constraints are graded and easy to miss:

- **Single-command start.** The whole pipeline (ingest → process → dashboard) must launch from one documented command, e.g. `docker compose up` or an equivalent script.
- **Committed, reproducible agent setup is a deliverable.** The tuned agentic-tool configuration (this `CLAUDE.md`, any skills, MCP configs, subagents, hooks, slash commands) must live in the repo with reproduction instructions. At minimum there must be a **custom skill (or equivalent)** that teaches the agent to interact with / analyze the dataset. Plain web-chat usage does not count.
- **Secrets via env vars only.** Never commit API keys/credentials; provide an `.env.example`.
- **Include** data validation + basic error handling, and document architecture decisions in a README.
- Consider **multiple personas** consuming the dashboards (e.g. engineering leadership vs. individual engineers), and frame insights so they could inform product decisions.

Optional bonus areas: ML for prediction/anomaly detection, real-time streaming, advanced stats, programmatic API endpoints.

## Generating the dataset

The generator uses the Python standard library only (no dependencies):

```bash
# default small dataset (writes to ./output)
python3 data/generate_fake_data.py

# realistic dataset (recommended baseline; matches `make data` / the Docker defaults)
python3 data/generate_fake_data.py --num-users 100 --num-sessions 5000 --days 60 --output-dir data/output
```

Flags: `--num-users` (default 30), `--num-sessions` (500), `--days` (30), `--output-dir` (`output`), `--seed` (42, deterministic). Data ends at a fixed `2026-02-01` and spans backward `--days`. Outputs `output/telemetry_logs.jsonl` and `output/employees.csv`.

## Data model (the part that needs reading multiple files to understand)

**`telemetry_logs.jsonl` is doubly-nested — parsing is not one `json.loads` per line.** Each line is a CloudWatch-style **batch**, and the real events are JSON *strings* inside it:

```
line (batch): { messageType, owner, logGroup, logStream, subscriptionFilters,
                year, month, day, logEvents: [ { id, timestamp(epoch ms), message } ] }
   └── message: JSON string → { body, attributes, scope, resource }
```

So ingestion is: read line → parse batch → for each `logEvents[i]` → `json.loads(message)` → event. The batch `year/month/day` are S3/Athena partition columns; trust `attributes["event.timestamp"]` (ISO-8601, ms precision) for actual event time.

**All numeric attribute values are stored as strings** (e.g. `"cost_usd": "0.071"`, `"input_tokens": "263"`) — cast on ingest.

**Event types** (`body` field, all prefixed `claude_code.`) and their distinguishing attributes:

| `body` | Key attributes |
|--------|----------------|
| `api_request` | `model`, `cost_usd`, `duration_ms`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` |
| `tool_decision` | `tool_name`, `decision` (`accept`/`reject`), `source` (`config`/`user_temporary`/`user_permanent`/`user_reject`) |
| `tool_result` | `tool_name`, `success` (`"true"`/`"false"`), `duration_ms`, `decision_type`, `decision_source`, optional `tool_result_size_bytes` |
| `user_prompt` | `prompt` (always `<REDACTED>`), `prompt_length` |
| `api_error` | `error`, `status_code`, `model`, `attempt`, `duration_ms` |

Every event also carries common attributes: `event.timestamp`, `session.id`, `user.email`, `user.id`, `user.account_uuid`, `organization.id`, `terminal.type`. The `resource` block adds host/OS/version and `user.practice`; `scope.version` is the Claude Code CLI version.

**Joining to employees:** `attributes["user.email"]` in telemetry ↔ `email` column in `employees.csv` (`email, full_name, practice, level, location`; `level` is `L1`–`L10`). Note the top-level `resource["user.email"]` is intentionally blank — use the one under `attributes`.

**Session model:** events share a `session.id`; a session is a sequence of turns, each = one `user_prompt` followed by interleaved `api_request` + `tool_decision`/`tool_result` cycles, with occasional `api_error` + retry. Useful for reconstructing session-level metrics (duration, cost, turns, tools per session). Event streams are generated with a business-hours bias (~70% during 9–17h).

## Analytics dimensions worth surfacing

Cost & token usage by model / practice / seniority level / user; tool adoption and accept-vs-reject rates by `source`; tool success rates and latency; error rates and status-code breakdown; session-level patterns (length, cost, turns); adoption over time and by terminal type / CLI version / OS. These map directly onto the "insights that inform product decisions" grading criterion.
