# Claude Code Usage Analytics Platform

An end-to-end analytics platform that ingests **Claude Code telemetry**, cleans and
models it, and serves **interactive dashboards** with cost, tool-usage, reliability,
and adoption insights for engineering leadership, FinOps, and platform/DX teams.

Built for the test task. The entire solution — pipeline, schema,
dashboard, tests, docs, and the agent configuration — is reproducible from source.

## Quick start (single command)

```bash
docker compose up --build
```

This generates a synthetic dataset (if none exists), builds the DuckDB database, serves
the dashboard at **http://localhost:8501**, and exposes a read-only REST API at
**http://localhost:8000** (interactive docs at `/docs`). Dataset size is configurable:

```bash
GEN_NUM_USERS=100 GEN_NUM_SESSIONS=20000 GEN_DAYS=90 docker compose up --build
```

### Prerequisites

`docker compose` needs the **Compose v2 plugin** and a **running Docker engine**
(the Docker CLI alone cannot start containers). Verify with `docker compose version`
and `docker info`.

**macOS** — use **Docker Desktop**, or a lightweight CLI engine via
[colima](https://github.com/abiosoft/colima):

```bash
brew install colima docker-buildx        # docker-buildx is required by Compose v2 to build
colima start                             # starts the Docker daemon
docker compose up --build
```

(To install just the Compose plugin manually, drop the release binary into
`~/.docker/cli-plugins/docker-compose` and `chmod +x` it.)

**Windows** — install **Docker Desktop** with the **WSL 2** backend, then run from
PowerShell or a WSL shell:

```powershell
docker compose up --build
```

To override the dataset size on PowerShell, set the env var first (the inline
`KEY=value cmd` form is bash-only):

```powershell
$env:GEN_NUM_SESSIONS=20000; docker compose up --build
```

### Local (without Docker)

**macOS / Linux:**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
make pipeline      # generate data + build the DuckDB database
make dashboard     # serve Streamlit at http://localhost:8501
make api           # (optional) serve the REST API at http://localhost:8000
```

**Windows (PowerShell)** — `make` isn't available by default, so run the steps directly:

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH = "src"
python data\generate_fake_data.py --num-users 100 --num-sessions 5000 --days 60 --output-dir data\output
python -m analytics.etl                                    # build the DuckDB database
streamlit run dashboard\app.py                             # dashboard at :8501
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000  # (optional) API at :8000
```

## Architecture

```
                    generate_fake_data.py            (synthetic source data)
                              │
        data/output/telemetry_logs.jsonl + employees.csv
                              │
   ┌──────────────────────────┴───────────────────────────┐
   │  ingest.py   parse double-nested JSONL, flatten,      │  src/analytics/
   │              cast strings→typed, validate/skip bad    │
   │  etl.py      load events + employees into DuckDB,      │
   │              build the events_enriched join view       │
   └──────────────────────────┬───────────────────────────┘
                              │
                    data/analytics.duckdb               (single source of truth)
                              │
                    queries.py   filterable, parameterized SQL → DataFrames
                       ┌──────────┴───────────┐
          dashboard/app.py                 api/main.py
       Streamlit + Plotly,             FastAPI read-only REST
       6 persona-oriented tabs         (same metrics, :8000/docs)
```

**Why these choices**

- **DuckDB** — embedded columnar SQL engine: zero server, one file, fast aggregation
  over the whole dataset. Ideal for a self-contained, reproducible analytics app, and
  it mirrors how this data would be queried in Athena/S3 in production.
- **Streamlit + Plotly** — interactive dashboards in pure Python with one command to
  serve; fast to build and easy for a reviewer to run.
- **Thin, layered Python** — ingest / ETL / queries are separated so each is testable
  in isolation and the query layer is reused verbatim by the dashboard, the API, and the
  tests.

### The key data challenge

`telemetry_logs.jsonl` is **doubly nested**: each line is a CloudWatch-style *batch*,
and the actual events are JSON **strings** inside `logEvents[].message`. Every numeric
attribute is stored as a string. `ingest.py` unpacks both layers, casts types, and
skips malformed batches/events while tallying them for validation. See
`.claude/skills/telemetry-analytics/SKILL.md` for the full data model.

## Data model

- **`events`** — one typed row per telemetry event (`api_request`, `tool_decision`,
  `tool_result`, `user_prompt`, `api_error`).
- **`employees`** — directory dimension (`email, full_name, practice, level, location`).
- **`events_enriched`** — `events` joined to `employees` on email, plus derived columns
  (date/week/hour, numeric seniority `level_num`, `total_tokens`). Queries build on this.

## Dashboard

| Tab | Persona | Highlights |
|-----|---------|-----------|
| Overview | Leadership | daily spend, active users, requests, day×hour activity heatmap |
| Cost | FinOps | spend by model, avg cost/request, cost per user by practice & seniority, top spenders |
| Tools | Platform/DX | most-used tools, success rates, rejection rates, authorization source |
| Reliability | Platform/DX | API error rate over time, errors by status code & message |
| Sessions & Adoption | All | session duration/cost distributions, adoption by terminal / CLI version / OS / location |
| Anomalies | FinOps / Platform/DX | daily cost & error-rate spikes flagged by a causal rolling z-score, with tunable window & threshold |

All charts respond to sidebar filters (date range, practice, seniority level, model).

## Programmatic access (API)

A **FastAPI** service exposes the same metrics as a read-only JSON API — the dashboard
and the API are two thin consumers of one query layer, so their numbers always agree.
It comes up with `docker compose up` on **http://localhost:8000**; run it alone with
`make api`. Interactive Swagger docs live at **`/docs`**.

| Endpoint | Returns |
|----------|---------|
| `GET /kpis` | headline KPIs (spend, requests, users, tokens, error rate) |
| `GET /cost/by-model` · `/cost/by-practice` · `/cost/by-level` | cost breakdowns |
| `GET /users/top?limit=` | top spenders |
| `GET /tools` · `/tools/decision-sources` | tool usage, success/reject, authorization mix |
| `GET /errors` · `/errors/rate` | error breakdown & rate over time |
| `GET /sessions/summary` | median/p90 session duration, turns, cost |
| `GET /adoption/{dimension}` | adoption by `terminal_type`/`cli_version`/`os_type`/`location` |
| `GET /anomalies/cost` · `/anomalies/errors` | daily series with anomalies flagged |
| `GET /filters` · `/health` | filter options; liveness |

Every list endpoint accepts the same filters as the dashboard, as query params:
`?start_date=2026-01-01&end_date=2026-01-31&practices=ML%20Engineering&models=claude-opus-4-6`
(`practices`/`levels`/`models` are repeatable). Example:

```bash
curl "http://localhost:8000/cost/by-model?practices=ML%20Engineering"
curl "http://localhost:8000/anomalies/cost?window=7&z_thresh=3"
```

## Commands

| Command | Description |
|---------|-------------|
| `make data` | Generate synthetic telemetry into `data/output/` |
| `make etl` | Build `data/analytics.duckdb` |
| `make pipeline` | `data` + `etl` |
| `make dashboard` | Serve the Streamlit dashboard |
| `make api` | Serve the FastAPI server (docs at `:8000/docs`) |
| `make test` | Run the test suite |
| `make clean` | Remove generated data and database |

Ad-hoc SQL: `duckdb data/analytics.duckdb` or `make etl` then query via `queries.connect()`.

## Testing & validation

`make test` covers the double-nesting unpack, type casting, required-field validation,
malformed-input skipping, a full ETL→query round-trip, the anomaly detector, and the API
endpoints (via `TestClient`). Runtime validation lives in the ETL (bad lines/events are
counted and reported; missing files raise actionable errors).

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request in two gated stages:

1. **`test`** — installs `requirements.txt` and runs the pytest suite.
2. **`docker`** — only if the tests pass: `docker compose build`, then brings the whole
   stack up with `docker compose up` (on a small dataset for speed), waits for the
   dashboard *and* API health checks, and smoke-tests a real `/kpis` + `/cost/by-model`
   response before tearing down.

So a green pipeline proves both that the code is correct and that the single-command
stack actually boots end-to-end.

This is **CI only — there is no CD (no delivery/deployment)**. The stack is brought up
*inside the ephemeral CI runner* purely to smoke-test it, then torn down; nothing is
published or exposed at a reachable URL. To actually use the app, run `docker compose up`
yourself and open the dashboard at `localhost:8501` and the API at `localhost:8000`.

## Agent setup (reproducible, committed)

This solution was built with **Claude Code**, tuned for the task via:

- **`CLAUDE.md`** — repo guide: the data-format trap, the schema, and the test task's
  hard constraints.
- **`.claude/skills/telemetry-analytics/`** — a custom skill teaching the agent the data
  model, query helpers, SQL patterns, and extension conventions so it can analyze the
  dataset and safely add metrics/charts.

No API keys are required (synthetic local data only). Configuration is via env vars;
see `.env.example`. Full write-up of what the agent generated, why the tuning helped,
and how to reproduce the setup: **[LLM_USAGE.md](LLM_USAGE.md)**.

## Project layout

```
src/analytics/     ingest.py · etl.py · queries.py · config.py   (pipeline + query API)
dashboard/app.py   Streamlit dashboard
api/main.py        FastAPI read-only REST layer
data/              generate_fake_data.py · DATASET.md · output/ (generated)
tests/             pytest suite
docker/            entrypoint.sh
.github/workflows/ ci.yml (tests → build → compose smoke test)
.claude/skills/    telemetry-analytics skill
ANALYTICS.md       metric definitions + headline findings (companion to the queries)
LLM_USAGE.md       agent setup, generated components, reproduction steps
presentation.pptx  brief deck: approach & findings
```

## Findings & presentation

- **[ANALYTICS.md](ANALYTICS.md)** — metric definitions (tied to `queries.py`) and the
  eight headline findings with the numbers behind them.
- **[presentation.pptx](presentation.pptx)** — editable slide deck of approach &
  findings. All figures are computed live from `data/analytics.duckdb`.

## Optional enhancements

- **Anomaly detection (implemented)** — the *Anomalies* tab flags daily cost and
  error-rate spikes with a causal rolling z-score (`queries.cost_anomalies` /
  `error_rate_anomalies`, unit-tested). Window and threshold are tunable in the UI.
- **API endpoints (implemented)** — the FastAPI service above gives programmatic,
  filterable access to every metric.

Further ideas: incremental ingest for streaming; per-user cohort retention; forecasting.
