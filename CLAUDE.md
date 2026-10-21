# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

This is a **test task** solution, not an existing product: an **AI Assistant Usage Analytics Platform** — an end-to-end system that ingests synthetic usage logs from four AI assistants (**Claude Code**, **ChatGPT / OpenAI**, **Gemini**, **DeepSeek**), maps them to one canonical event schema, extracts usage/cost/reliability insights, and presents them in an interactive dashboard and a REST API. It started as Claude-Code-only analytics and was extended to multiple providers.

Everything is authored in this repo: pipeline, provider adapters, dashboard, API, tests, Docker/CI, docs and agent setup. The dataset generator `data/generate_fake_data.py` was provided with the test task and has since been **extended** (model catalog + three more providers) under one hard rule: for the same arguments its Claude Code output is byte-identical to the original, apart from the `uuid4` session/account/org ids, which the test pins (see "Generating the dataset").

**Synthetic data only.** There are no real provider APIs, connectors or keys anywhere — every provider format is produced by the seeded generator.

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

Flags: `--num-users` (default 30), `--num-sessions` (500, Claude Code sessions; the other providers scale with the same sessions-per-user-per-day density), `--days` (30), `--output-dir` (`output`), `--seed` (42), `--providers` (comma-separated subset of `claude_code,openai,gemini,deepseek`; default/empty = all four; an unknown name is an error). Data ends at a fixed `2026-02-01` and spans backward `--days`.

Outputs: `telemetry_logs.jsonl` (Claude Code; only if `claude_code` is selected), `employees.csv` (always — shared by every provider), `openai_usage.jsonl`, `gemini_usage.jsonl`, `deepseek_usage.csv`.

- `make data` / `docker compose up` read `GEN_NUM_USERS`, `GEN_NUM_SESSIONS`, `GEN_DAYS`, `GEN_SEED`, `GEN_PROVIDERS` (empty = all). The ETL requires the Claude Code file, so the Docker entrypoint refuses a first start whose `GEN_PROVIDERS` excludes `claude_code`; on a data volume that already exists it generates only the missing provider files.
- **Determinism / Claude Code guarantee.** The other providers run *after* Claude Code, each on its own `random.Random(seed + offset)`, so they never consume the global `random` stream. The Claude Code files are therefore exactly what the original generator wrote. The one non-seeded input is the generator's own `uuid.uuid4()` (session/account/org ids), so Claude Code files are only byte-identical run to run with `uuid4` pinned. The provider files are byte-identical for a given seed, and stable for any `--providers` subset. `tests/test_generator.py` enforces this. It re-runs the original generator from git commit `1f1a5df` and compares bytes, falling back to frozen float-normalised digests on a shallow clone. CI checks out full history for this test.
- Model parameters live in the generator's `MODEL_CATALOG`, grouped by provider: weight, illustrative USD prices per 1M tokens (input / cached input / output), token and latency profiles, error rate. Practice-level adoption weights live in `PROVIDER_ADOPTION`.

## Data model (the part that needs reading multiple files to understand)

Every provider has its **own raw format**; an adapter in `src/analytics/providers/` maps it to canonical rows. The core (ETL, queries, API, dashboard) only sees canonical rows.

### Claude Code — `telemetry_logs.jsonl` (the trap)

**Doubly nested — parsing is not one `json.loads` per line.** Each line is a CloudWatch-style **batch**, and the real events are JSON *strings* inside it:

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

**Session model:** events share a `session.id`; a session is a sequence of turns, each = one `user_prompt` followed by interleaved `api_request` + `tool_decision`/`tool_result` cycles, with occasional `api_error` + retry. Event streams are generated with a business-hours bias (~70% during 9–17h). Tool decisions, prompts/turns and terminal/CLI/OS details exist **only** for Claude Code.

### Other providers (one record per request, real JSON/CSV numbers — still cast defensively)

- **OpenAI — `openai_usage.jsonl`**: Chat Completions-style record plus gateway metadata. `created` is **epoch seconds**; `user` = email; `client` ∈ `chatgpt_enterprise`/`api`; `conversation_id` (null for raw API calls); `latency_ms`; `usage.prompt_tokens` **includes** `usage.prompt_tokens_details.cached_tokens`; `completion_tokens` **includes** `completion_tokens_details.reasoning_tokens`; `tool_calls[]` become extra `tool_result` rows with `success` NULL; failures have `error: {type, code, status}` and `usage: null`. A few lines are exact gateway replays (removed by de-duplication).
- **Gemini — `gemini_usage.jsonl`**: Vertex AI-style, **camelCase** `usageMetadata` (`promptTokenCount`, `candidatesTokenCount`, `cachedContentTokenCount`, `thoughtsTokenCount`, `totalTokenCount`; zero counts are omitted). `principalEmail`, `modelVersion`, `surface` ∈ `gemini_app`/`gemini_code_assist`/`api`, `sessionId` (absent for `api`), `latencyMs`, ISO `timestamp`. **`thoughtsTokenCount` is reported outside `candidatesTokenCount`** but billed as output. Errors: non-200 `status.code` + `status.message`, no `usageMetadata`. A few lines are deliberately truncated (counted as skipped).
- **DeepSeek — `deepseek_usage.csv`**: gateway CSV `request_id,timestamp,user_email,model,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost_usd,latency_ms,status_code,error`. Cache hit/miss are separate columns; `completion_tokens` includes reasoning; no session id. Errors: `status_code` ≠ 200 with empty token columns.

Every record of every format carries `cost_usd`, **generated per record** from the model catalog as `(uncached × input + cached × cached_input + output × output price) / 1e6`, where output already includes reasoning tokens. Failed requests cost 0. Claude Code keeps its original sampled cost distribution. The ETL never estimates cost. A missing or malformed cost stays NULL and is reported in `ingest_runs` / `data_quality`.

### Canonical schema (`events` table, one wide table for all providers)

The original 30 Claude Code columns are unchanged (names and types). The multi-provider columns are `provider` (NOT NULL: `claude_code`/`openai`/`gemini`/`deepseek`), `client` (surface: `claude_code_cli`, `chatgpt_enterprise`, `gemini_app`, `gemini_code_assist`, `api`), `model_family` (dated/revision suffixes stripped by `providers/models.py`, e.g. `gpt-4o-2024-08-06` → `gpt-4o`), `source_event_id` (de-dup key with `provider`), `reasoning_tokens`. Successful requests → `event_type='api_request'`, failures → `'api_error'` (`status_code` VARCHAR). Sources without a session id get a synthetic one: same person, 30-minute inactivity gap.

**Token mapping** (canonical ← raw):

| canonical | Claude Code | OpenAI | Gemini | DeepSeek |
|---|---|---|---|---|
| `input_tokens` (uncached) | `input_tokens` | `prompt_tokens − cached_tokens` | `promptTokenCount − cachedContentTokenCount` | `prompt_cache_miss_tokens` |
| `cache_read_tokens` | `cache_read_tokens` | `cached_tokens` | `cachedContentTokenCount` | `prompt_cache_hit_tokens` |
| `cache_creation_tokens` | `cache_creation_tokens` | NULL | NULL | NULL |
| `output_tokens` | `output_tokens` | `completion_tokens` | `candidatesTokenCount + thoughtsTokenCount` | `completion_tokens` |
| `reasoning_tokens` | NULL | `reasoning_tokens` | `thoughtsTokenCount` | `reasoning_tokens` |

`reasoning_tokens` is a **subset of** `output_tokens`, never added on top. `events_enriched.total_tokens` = input + output + cache_read + cache_creation with `COALESCE(…, 0)` per term, because one NULL term must not NULL the sum.

Other tables: `employees`, `provider_labels`, `ingest_runs` (per-provider ETL audit: rows read/loaded/skipped, duplicates, unpriced models), and the view `events_enriched` (events ⋈ employees ⋈ labels + derived date/hour/level columns).

**Joining to employees:** the per-row `user_email` ↔ `email` in `employees.csv` (`email, full_name, practice, level, location`; `level` is `L1`–`L10`). For Claude Code use `attributes["user.email"]`; the top-level `resource["user.email"]` is intentionally blank.

## Analytics dimensions worth surfacing

Cost & token usage by provider / model family / practice / seniority level / user; provider comparison (share of spend/requests/users over time, cost per 1K tokens, error rate, p50/p95 latency, multi-provider users); tool adoption and accept-vs-reject rates by `source`; tool success rates and latency; error rates and status-code breakdown; session-level patterns (length, cost, turns); adoption over time and by client / terminal type / CLI version / OS. Tool, turn and terminal metrics are **Claude Code only**. These map directly onto the "insights that inform product decisions" grading criterion. Query conventions are in `.claude/skills/telemetry-analytics/SKILL.md`.

## Subagents

Three project subagents live in `.claude/agents/`:

| Agent | Owns | Must not touch |
|---|---|---|
| `backend-engineer` | `src/`, `api/`, `tests/`, `data/generate_fake_data.py`, `data/DATASET.md`, `docker/`, `Makefile`, `docker-compose.yml`, `.env.example`, `.github/workflows/` | `dashboard/` |
| `frontend-engineer` | `dashboard/` only (data comes from `queries.py`; no SQL in the dashboard) | everything else; asks the backend for new query functions |
| `code-reviewer` | nothing — read-only review (secrets, metric correctness, backward compatibility, safety, tests) | all files |

Working rule: every phase of work lands as **one PR into `main`**, merged only after `code-reviewer` approval and green CI. Backend and frontend agree on the contract first (schema, `Filters` fields, query signatures) and can then work in parallel.

Planning specs live locally under `docs/SPEC_*.md` (git-ignored); agents should read them if present and never commit them.
