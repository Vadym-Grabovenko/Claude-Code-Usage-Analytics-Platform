---
name: telemetry-analytics
description: >-
  Query, analyze, and extend the Claude Code telemetry analytics platform in this
  repo. Use whenever the task involves the telemetry dataset (telemetry_logs.jsonl,
  employees.csv), the DuckDB database (data/analytics.duckdb), the ingest/ETL/query
  code under src/analytics, or the Streamlit dashboard — e.g. computing a usage/cost
  metric, adding a chart, writing an ad-hoc SQL analysis, or debugging the pipeline.
---

# Claude Code Telemetry Analytics

This skill gives you the data model, query patterns, and conventions needed to work
productively with this analytics platform without re-deriving them from source.

## Architecture in one line

`telemetry_logs.jsonl` (+ `employees.csv`) → **ingest** (`src/analytics/ingest.py`,
normalize) → **ETL** (`src/analytics/etl.py`, load into DuckDB) → **queries**
(`src/analytics/queries.py`) → **dashboard** (`dashboard/app.py`, Streamlit).

The DuckDB file `data/analytics.duckdb` is the single source of truth for all
analysis. Always query it rather than re-parsing the JSONL, unless you are changing
ingestion itself.

## The data trap: double nesting

`telemetry_logs.jsonl` is **not** one event per line. Each line is a CloudWatch
batch; real events are JSON **strings** inside `logEvents[].message`:

```
line → {logEvents: [{message: "{...event...}"}]}   # message must be json.loads'd again
event → {body, attributes, scope, resource}
```

`ingest.iter_events()` already handles this and yields flat, typed dicts. **All raw
numeric attributes are strings** (`"cost_usd": "0.071"`) — the normalizer casts
them; if you touch raw data yourself, cast explicitly.

## Query the database

Preferred — use the connection + query helpers:

```python
import sys; sys.path.insert(0, "src")
from analytics import queries
from analytics.queries import Filters

con = queries.connect()                 # read-only DuckDB connection
queries.kpis(con, Filters())            # headline metrics
queries.cost_by_model(con, Filters(practices=["ML Engineering"]))
```

Ad-hoc SQL (fast for exploration):

```bash
PYTHONPATH=src python -c "from analytics import queries as q; \
print(q.connect().execute('SELECT event_type, count(*) FROM events GROUP BY 1 ORDER BY 2 DESC').df())"
```

Or open it directly: `duckdb data/analytics.duckdb`.

### Tables & the view you almost always want

- `events` — one row per telemetry event, wide/typed (see columns below).
- `employees` — dimension: `email, full_name, practice, level, location`.
- `events_enriched` — **use this**: `events` LEFT JOINed to `employees` on
  `user_email = email`, plus derived columns: `practice`, `level`, `level_num`
  (L5→5), `event_date`, `event_week`, `event_hour`, `event_dow`, `total_tokens`.

### Event types (`event_type` column) and their meaningful fields

| event_type      | fields that are populated |
|-----------------|---------------------------|
| `api_request`   | `model`, `cost_usd`, `duration_ms`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` |
| `tool_decision` | `tool_name`, `decision` (accept/reject), `decision_source` (config/user_temporary/user_permanent/user_reject) |
| `tool_result`   | `tool_name`, `success` (bool), `duration_ms`, `decision_type`, `tool_result_size_bytes` |
| `user_prompt`   | `prompt_length` (prompt text is always `<REDACTED>`) |
| `api_error`     | `error`, `status_code`, `model`, `attempt`, `duration_ms` |

Common to every row: `timestamp`, `session_id`, `user_email`, `terminal_type`,
`cli_version`, `os_type`, `host_arch`. **Filter by `event_type` before aggregating
a type-specific column** (e.g. only `api_request` rows have `cost_usd`).

## Query patterns that come up constantly

- **Total spend:** `sum(cost_usd)` (only `api_request` rows have it; others are NULL so a global sum is safe).
- **A session** = rows sharing `session_id`; duration = `max(timestamp)-min(timestamp)`; turns = count of `user_prompt`; see `queries.session_stats`.
- **Tool accept vs reject:** `tool_decision` rows; reject rate = rejects / decisions per `tool_name`.
- **Tool reliability:** `avg(success::INT)` over `tool_result` rows per `tool_name`.
- **Cost per user by cohort:** `sum(cost_usd)/count(DISTINCT user_email)` grouped by `practice` or `level`.
- **Ordering seniority:** sort by `level_num`, not the `L#` string.
- **Anomalies:** `cost_anomalies` / `error_rate_anomalies` return a daily series plus
  `baseline`, `z_score`, `is_anomaly` from a causal rolling z-score; the detector core is
  the pure, unit-tested `detect_anomalies(series_df, value_col, window, z_thresh)`.

## Conventions when extending

- **New metric** → add a function to `queries.py` following the existing shape:
  take `(con, f: Filters, ...)`, aggregate over `events_enriched`, use the
  `{where}` placeholder + `_q()` so dashboard filters apply, return a DataFrame.
- **Never string-format user/filter values into SQL** — use bind params (`?`).
  The one exception, column names, must be checked against an allow-list (see
  `adoption_by_dimension`).
- **New chart** → add to the relevant tab in `dashboard/app.py` using Plotly
  Express + `st.plotly_chart(..., use_container_width=True)`.
- **Schema change in ingest** → update `normalize_event` and add a case to
  `tests/test_ingest.py`; the ETL rebuilds the DB from scratch each run.
- After code changes, run `PYTHONPATH=src python -m pytest -q` and rebuild with
  `make etl`.

## Regenerate / rebuild

```bash
make data       # regenerate synthetic telemetry into data/output/
make etl        # rebuild data/analytics.duckdb
make pipeline   # both
make dashboard  # serve Streamlit locally
```

Dataset knobs (env or make vars): `GEN_NUM_USERS`, `GEN_NUM_SESSIONS`, `GEN_DAYS`,
`GEN_SEED`. The generator is seeded, so the dataset is reproducible.

## Analytical framing (personas)

When asked for "insights", target the consumer:
- **Leadership** — spend trend, active users, adoption over time.
- **FinOps** — cost by model/practice/level, top spenders, cost per user.
- **Platform/DX** — tool adoption, accept-vs-reject, success rates, error mix.
Tie findings to a decision (model routing, enablement, guardrails), not just a number.
