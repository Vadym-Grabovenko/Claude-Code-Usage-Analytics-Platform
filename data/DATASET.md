# AI Assistant Usage Dataset

Synthetic usage data for four AI assistants used by the same (synthetic) employees:
**Claude Code** (Anthropic's CLI), **ChatGPT / OpenAI**, **Gemini** and **DeepSeek**.

## Quick Start

No dependencies required — uses Python standard library only.

```bash
python3 generate_fake_data.py
```

For a realistic dataset, generate at least 100 engineers over a couple of months:

```bash
python3 generate_fake_data.py --num-users 100 --num-sessions 5000 --days 60
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--num-users` | 30 | Number of engineers |
| `--num-sessions` | 500 | Total Claude Code sessions (other providers scale with the same density) |
| `--days` | 30 | Time span in days (data ends 2026-02-01) |
| `--output-dir` | `output` | Output directory |
| `--seed` | 42 | Random seed for reproducibility |
| `--providers` | `claude_code,openai,gemini,deepseek` | Providers to generate |

**Reproducibility.** The Claude Code files are exactly what the original Claude-Code-only
generator produced for the same arguments: the other providers use their own
`random.Random(seed + offset)` and run after Claude Code, so they never consume the
Claude Code random stream. (Claude Code session/account ids come from `uuid.uuid4()` and
differ between runs; everything else is seeded. The provider files are byte-identical for
a given seed.)

## Output Files

| File | Provider | Format | Description |
|------|----------|--------|-------------|
| `telemetry_logs.jsonl` | Claude Code | JSONL | CloudWatch-style telemetry log batches |
| `employees.csv` | — | CSV | Employee directory, shared by all providers |
| `openai_usage.jsonl` | ChatGPT / OpenAI | JSONL | One Chat Completions-style record per request |
| `gemini_usage.jsonl` | Gemini | JSONL | One Vertex AI / Gemini API log record per request |
| `deepseek_usage.csv` | DeepSeek | CSV | API gateway log, one row per request |

Every record carries a `cost_usd` computed by the generator as
`(uncached input × input price + cached input × cached price + output × output price) / 1e6`
from the model catalog (`MODEL_CATALOG` in the generator; prices per 1M tokens are
**illustrative**, not vendor quotes). Failed requests carry `cost_usd = 0`. Claude Code keeps
its own reported-cost distribution.

## Claude Code telemetry structure

Each log record contains a batch of `logEvents`. Each event has a JSON `message` with:

- `body` — event type
- `attributes` — event-specific fields
- `scope` — instrumentation metadata
- `resource` — host/user environment info

## OpenAI — `openai_usage.jsonl`

```json
{"id":"chatcmpl-…","object":"chat.completion","created":1767605400,
 "model":"gpt-4o-2024-08-06","user":"alex.chen@example.com","client":"chatgpt_enterprise",
 "conversation_id":"conv_…","latency_ms":1830,
 "usage":{"prompt_tokens":812,"completion_tokens":240,"total_tokens":1052,
          "prompt_tokens_details":{"cached_tokens":512},
          "completion_tokens_details":{"reasoning_tokens":0}},
 "tool_calls":[{"type":"function","name":"code_interpreter"}],"cost_usd":0.0043,"error":null}
```

- `created` is epoch seconds; `user` is the employee email.
- `client` ∈ `chatgpt_enterprise`, `api`. Raw API calls have `conversation_id: null`.
- Errors: `"error":{"type":"rate_limit_error","code":"rate_limit_exceeded","status":429}`, `usage: null`.
- Models: `gpt-4o`, `gpt-4o-mini`, `o3`, `o4-mini` (dated snapshot ids).
- `completion_tokens` already includes `reasoning_tokens`.

## Gemini — `gemini_usage.jsonl`

```json
{"timestamp":"2026-01-05T09:30:00.120Z","responseId":"…","principalEmail":"alex.chen@example.com",
 "modelVersion":"gemini-2.5-pro","surface":"gemini_code_assist","sessionId":"g-123","latencyMs":2100,
 "usageMetadata":{"promptTokenCount":900,"candidatesTokenCount":300,
                  "cachedContentTokenCount":100,"thoughtsTokenCount":120,"totalTokenCount":1320},
 "cost_usd":0.0052,"status":{"code":200}}
```

- `surface` ∈ `gemini_app`, `gemini_code_assist`, `api` (`api` records have no `sessionId`).
- `cachedContentTokenCount` / `thoughtsTokenCount` are omitted when zero.
- `thoughtsTokenCount` is **not** part of `candidatesTokenCount` (total = prompt + candidates + thoughts) but is billed as output.
- Errors: `"status":{"code":429,"message":"RESOURCE_EXHAUSTED"}`, no `usageMetadata`.

## DeepSeek — `deepseek_usage.csv`

```
request_id,timestamp,user_email,model,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost_usd,latency_ms,status_code,error
```

- Models: `deepseek-chat`, `deepseek-reasoner`. `completion_tokens` includes `reasoning_tokens`.
- Errors: `status_code` ≠ 200 with an `error` message and empty token columns.
- No session id — the ETL groups requests into 30-minute-gap sessions.

## How the formats map to one schema

The ETL (`src/analytics/providers/`) maps every format to one row per event with a
`provider` column (`claude_code`, `openai`, `gemini`, `deepseek`):

| canonical | Claude Code | OpenAI | Gemini | DeepSeek |
|---|---|---|---|---|
| `input_tokens` (uncached) | `input_tokens` | `prompt_tokens − cached_tokens` | `promptTokenCount − cachedContentTokenCount` | `prompt_cache_miss_tokens` |
| `cache_read_tokens` | `cache_read_tokens` | `cached_tokens` | `cachedContentTokenCount` | `prompt_cache_hit_tokens` |
| `cache_creation_tokens` | `cache_creation_tokens` | — | — | — |
| `output_tokens` | `output_tokens` | `completion_tokens` | `candidatesTokenCount + thoughtsTokenCount` | `completion_tokens` |
| `reasoning_tokens` (part of output) | — | `reasoning_tokens` | `thoughtsTokenCount` | `reasoning_tokens` |
| `session_id` | `session.id` | `conversation_id` or synthetic | `sessionId` or synthetic | synthetic |
| `client` | `claude_code_cli` | `client` | `surface` | `api` |

Successful requests become `api_request`, failures `api_error`; OpenAI `tool_calls` become
`tool_result` rows with `success` unknown. Tool decisions, prompts and terminal/CLI/OS
details exist only for Claude Code.

## Built-in patterns

- Provider adoption depends on practice (e.g. ML/Data Engineering use more Gemini,
  Frontend/Backend more ChatGPT); not everyone uses every provider.
- Gemini usage ramps up over the period (a growing share).
- A one-day burst of OpenAI 429 rate-limit errors about a third of the way back from the end.
- A small group of power users on every provider, at higher volume.
- Data-quality noise: a few replayed OpenAI records (exact duplicates) and a few truncated
  Gemini lines.
- Business-hours bias (~70% of sessions during 9–17h), like Claude Code.

## Employee Table

| Column | Description |
|--------|-------------|
| email | Employee email |
| full_name | Full name |
| practice | Engineering practice |
| level | Seniority level (L1–L10) |
| location | Country |

## Notes

- All user identifiers are synthetic
- Prompt contents are redacted
- Employee emails match the telemetry data of every provider
- No real provider APIs are called and no API keys are needed
