---
name: telemetry-analytics
description: >-
  Query, analyze, and extend the multi-provider AI usage analytics platform in this
  repo (Claude Code, ChatGPT/OpenAI, Gemini, DeepSeek). Use whenever the task involves
  the synthetic usage data (telemetry_logs.jsonl, openai_usage.jsonl, gemini_usage.jsonl,
  deepseek_usage.csv, employees.csv), the DuckDB database (data/analytics.duckdb), the
  provider adapters / ingest / ETL / query code under src/analytics, the FastAPI layer,
  or the Streamlit dashboard — e.g. computing a usage/cost metric, comparing providers,
  adding a chart, writing an ad-hoc SQL analysis, or debugging the pipeline.
---

# AI Assistant Usage Analytics

This skill gives you the data model, query patterns, and conventions needed to work
productively with this analytics platform without re-deriving them from source.

## Architecture in one line

raw usage logs per provider (+ `employees.csv`) → **provider adapters**
(`src/analytics/providers/`, raw → canonical rows; Claude Code wraps
`src/analytics/ingest.py`) → **ETL** (`src/analytics/etl.py`, one wide `events` table
in DuckDB, de-dup, audit) → **queries** (`src/analytics/queries.py`) →
**dashboard** (`dashboard/app.py`, Streamlit) and **API** (`api/main.py`, FastAPI).

The DuckDB file `data/analytics.duckdb` is the single source of truth for all
analysis. Always query it rather than re-parsing raw files, unless you are changing
ingestion itself. All data is **synthetic** (seeded generator, illustrative prices).

## The data traps

- **Claude Code JSONL is doubly nested.** Each line is a CloudWatch batch; real events
  are JSON **strings** inside `logEvents[].message` (`json.loads` twice). **All raw
  numeric attributes are strings** (`"cost_usd": "0.071"`). `ingest.iter_events()`
  handles this and yields flat, typed dicts.
- **OpenAI** `prompt_tokens` includes the cached tokens; `completion_tokens` includes
  reasoning. `created` is epoch seconds.
- **Gemini** is camelCase `usageMetadata`; `thoughtsTokenCount` is *outside*
  `candidatesTokenCount`, so the adapter sets `output_tokens = candidates + thoughts`.
- **DeepSeek** CSV splits prompt tokens into cache hit / miss columns; no session id.
- Every adapter emits the **same key set** (`providers.base.empty_row()`); the canonical
  invariants are documented at the top of `providers/base.py`. If you touch raw data
  yourself, cast every numeric field explicitly.

## Query the database

Preferred — use the connection + query helpers:

```python
import sys; sys.path.insert(0, "src")
from analytics import queries
from analytics.queries import Filters

con = queries.connect()                                 # read-only DuckDB connection
queries.kpis(con, Filters())                            # all providers
queries.provider_summary(con, Filters())                # one row per provider
queries.cost_by_model(con, Filters(providers=["claude_code"], practices=["ML Engineering"]))
```

Ad-hoc SQL (fast for exploration):

```bash
PYTHONPATH=src python -c "from analytics import queries as q; \
print(q.connect().execute('SELECT provider, event_type, count(*) FROM events GROUP BY ALL ORDER BY 1, 3 DESC').df())"
```

Or open it directly: `duckdb data/analytics.duckdb`.

### Tables & the view you almost always want

- `events` — one row per event, all providers, wide/typed (columns below).
- `employees` — dimension: `email, full_name, practice, level, location`.
- `provider_labels` — `provider → provider_label` (e.g. `openai → ChatGPT / OpenAI`).
- `ingest_runs` — latest ETL audit per provider: `source_file, status` (`ok`/`missing`),
  `rows_read, rows_loaded, rows_skipped, duplicates_removed, unknown_models`.
- `events_enriched` — **use this**: `events` LEFT JOIN `employees` on
  `user_email = email` and `provider_labels`, plus `provider_label`, `full_name`,
  `practice`, `level`, `level_num` (L5→5), `event_date`, `event_week`, `event_hour`,
  `event_dow`, `total_tokens` (COALESCE per term).

### Columns by event type

| event_type      | populated fields | providers |
|-----------------|------------------|-----------|
| `api_request`   | `model`, `model_family`, `cost_usd`, `duration_ms`, `input_tokens` (uncached), `output_tokens` (incl. reasoning), `cache_read_tokens`, `cache_creation_tokens` (CC only), `reasoning_tokens` (not CC) | all |
| `api_error`     | `error`, `status_code` (VARCHAR), `model`, `duration_ms`; CC also `attempt` | all |
| `tool_decision` | `tool_name`, `decision` (accept/reject), `decision_source` | Claude Code |
| `tool_result`   | `tool_name`, `success`, `duration_ms`, `decision_type`, `tool_result_size_bytes` | Claude Code; OpenAI `tool_calls` as `tool_result` with `success` NULL |
| `user_prompt`   | `prompt_length` (text always `<REDACTED>`) | Claude Code |

Common to every row: `provider`, `client`, `timestamp`, `session_id`, `user_email`,
`source_event_id`. Claude Code only: `terminal_type`, `cli_version`, `os_type`,
`host_arch`. **Filter by `event_type` before aggregating a type-specific column.**

## Filters

`Filters(start_date, end_date, practices, levels, models, providers, model_families)`;
empty = no filter. **With no `providers` filter every metric covers all providers.**

- `providers` — e.g. `["claude_code", "openai"]`; valid ids: `queries.valid_providers()`.
- `models` — **raw** model ids (e.g. `gpt-4o-2024-08-06`).
- `model_families` — normalized families (e.g. `gpt-4o`); use these for user-facing
  model pickers (`filter_options()["models_by_provider"]` lists them per provider).

`filter_options(con)` returns the widget options: dates, practices, levels, `models`,
`providers`, `provider_labels`, `model_families`, `models_by_provider`, `clients`.

## Query patterns that come up constantly

- **Total spend:** `sum(cost_usd)` (only `api_request` rows have it).
- **Provider comparison:** `provider_summary` — requests, errors, `error_rate`
  (api_error ÷ api_request), users, sessions, cost, token sums, p50/p95 latency of
  `api_request`, `cost_per_user`, `cost_per_request`, `cost_per_1k_tokens`
  (cost ÷ tokens of priced requests).
- **Provider share over time:** `provider_share_over_time(grain="day"|"week"|"month")`
  → absolute + `cost_share` / `requests_share` / `users_share` per period.
- **Practice/level × provider:** `provider_by_dimension(column)`, allow-listed
  `practice`, `level`, `location`, `client`, `model_family`.
- **Multi-provider users:** `multi_provider_users` → `{"distribution", "pairs"}` (users
  by number of providers; pairwise overlap + Jaccard).
- **Model leaderboard:** `model_leaderboard(limit=None)` — per provider × model family:
  requests, errors, cost, unit cost, latency, first/last used.
- **Provider catalog:** `provider_catalog` — every known provider + `has_data`.
- **Data quality:** `data_quality(con, f)` — `ingest_runs` + live unpriced-request and
  unknown-employee counts.
- **Anomalies:** `cost_anomalies` / `error_rate_anomalies` (and `error_rate_over_time`)
  take `by_provider=True` to score each provider's series separately; the detector core
  is the pure, unit-tested `detect_anomalies(series_df, value_col, window, z_thresh)`.
- **Claude-Code-only metrics:** `tool_usage`, `decision_sources`, `kpis()["prompts"]`,
  and `session_stats` `turns` / `tool_calls`. Other providers' sessions have 0 turns, so
  **pass `providers=["claude_code"]`** (or slice the frame) before taking session
  medians, or they collapse toward 0.
- **A session** = rows sharing `session_id`; duration = `max(timestamp)-min(timestamp)`.
- **Tool accept vs reject:** `tool_decision` rows; reject rate = rejects / decisions.
- **Cost per user by cohort:** `sum(cost_usd)/count(DISTINCT user_email)` by `practice`/`level`.
- **Ordering seniority:** sort by `level_num`, not the `L#` string.
- **Cost per 1K tokens across providers:** Claude Code tokens are ~90% cache reads,
  which makes its blended $/1K look cheap; compare on uncached input + output as well
  before concluding anything.

## Conventions when extending

- **New metric** → add a function to `queries.py` following the existing shape:
  take `(con, f: Filters, ...)`, aggregate over `events_enriched`, use the
  `{where}` placeholder + `_q()` so filters apply, return a DataFrame.
- **Never string-format user/filter values into SQL** — use bind params (`?`).
  The one exception, column names, must be checked against an allow-list (see
  `adoption_by_dimension`, `provider_by_dimension`).
- **Token sums:** `COALESCE(col, 0)` per term; never add `reasoning_tokens` to output.
- **New provider** → a `BaseAdapter` subclass in `src/analytics/providers/`, rows built
  from `empty_row()`, registered in `REGISTRY`; add its format to the generator's
  `MODEL_CATALOG` with its own seeded `random.Random`; tests in `tests/test_providers.py`.
  A missing optional provider file is a warning, not a failure.
- **New chart** → add to the relevant tab in `dashboard/app.py` using Plotly
  Express + `st.plotly_chart(fig, width="stretch")` (not the deprecated
  `use_container_width`). Color by `provider` with the shared `PROVIDER_COLORS` when
  more than one provider is in view; CC-only charts show `st.info` when Claude Code
  isn't selected.
- **New API endpoint** → a thin wrapper in `api/main.py` over a query function, filters
  via `Depends(get_filters)` (unknown provider → 422).
- **Schema change in ingest** → update `normalize_event` / the adapter, add cases to
  `tests/test_ingest.py` / `tests/test_providers.py`; the ETL rebuilds the DB from
  scratch each run. Existing tests must stay unmodified and green.
- After code changes, run `PYTHONPATH=src python -m pytest -q` and rebuild with
  `make etl`.

## Regenerate / rebuild

```bash
make data       # regenerate synthetic data for GEN_PROVIDERS into data/output/
make etl        # rebuild data/analytics.duckdb
make pipeline   # both
make dashboard  # serve Streamlit locally
make api        # serve the REST API (docs at :8000/docs)
```

Dataset knobs (env or make vars): `GEN_NUM_USERS`, `GEN_NUM_SESSIONS`, `GEN_DAYS`,
`GEN_SEED`, `GEN_PROVIDERS` (empty = all four). The ETL loads `PROVIDERS` (default all).
The generator is seeded, so the dataset is reproducible.

## Analytical framing (personas)

When asked for "insights", target the consumer:
- **Leadership** — spend trend, active users, adoption over time; **provider share**
  of spend / requests / users and whether a provider is gaining (`provider_share_over_time`).
- **FinOps** — cost by model/practice/level, top spenders, cost per user; **cost per 1K
  tokens and per request by provider** (`provider_summary`), model leaderboard, who pays
  for several providers (`multi_provider_users`).
- **Platform/DX** — tool adoption, accept-vs-reject, success rates, error mix; **error
  rate and p50/p95 latency per provider**, per-provider anomalies (`by_provider=True`).
- **Engineering manager** — practice/level × provider (`provider_by_dimension`).
Tie findings to a decision (model routing, consolidation, quota/retry, enablement,
guardrails), not just a number. State that prices are illustrative.
