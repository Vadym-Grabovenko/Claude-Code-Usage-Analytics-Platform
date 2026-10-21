# AI Assistant Usage Analytics Platform

An end-to-end analytics platform for the AI coding assistants a company uses:
**Claude Code**, **ChatGPT / OpenAI**, **Gemini** and **DeepSeek**. It ingests each
provider's usage logs in that provider's own format, maps them to one canonical event
schema, and serves an **interactive dashboard** and a **read-only REST API**. Both cover
cost, tokens, reliability, tool usage and adoption. Views include per-provider
comparisons for engineering leadership, FinOps and platform/DX teams.

All data is **synthetic**: a seeded, standard-library generator produces realistic logs
for all four providers. No real provider API is called and no API key is needed.

Built for the test task, which started as Claude Code-only analytics and was then
extended to several providers. The whole solution is reproducible from source:
pipeline, adapters, schema, dashboard, API, tests, docs and the agent configuration.

## Quick start (single command)

```bash
docker compose up --build
```

This generates a synthetic dataset for all four providers (if none exists), builds the
DuckDB database, serves the dashboard at **http://localhost:8501**, and exposes a
read-only REST API at **http://localhost:8000** (interactive docs at `/docs`). Dataset
size and providers are configurable:

```bash
GEN_NUM_USERS=100 GEN_NUM_SESSIONS=20000 GEN_DAYS=90 docker compose up --build
GEN_PROVIDERS=claude_code,openai docker compose up --build   # empty = all four
```

`GEN_PROVIDERS` must include `claude_code` on a first start, because the ETL requires
the Claude Code dataset. If `./data` already holds a dataset from before the
multi-provider extension, startup generates only the missing provider files and leaves
the existing ones untouched.

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

**Linux, upgrading from an older image that ran as root:** files it wrote under `./data`
are root-owned. The new non-root container stops with an error that names them. Reclaim
them once with `sudo chown -R $USER data/`.

### Local (without Docker)

Dependencies are installed with the same pins as the Docker image and CI
(`-c constraints.txt`). The pins were verified on Python 3.12 (image and CI) and also
resolve to binary wheels on Python 3.14. If a pin has no wheel for your Python, drop
`-c constraints.txt` to install within the version ranges in `requirements*.txt`.

**macOS / Linux:**

```bash
python3 -m venv .venv && source .venv/bin/activate
make install       # pip install -r requirements-dev.txt -c constraints.txt
make pipeline      # generate data for all providers + build the DuckDB database
make dashboard     # serve Streamlit at http://localhost:8501
make api           # (optional) serve the REST API at http://localhost:8000
```

**Windows (PowerShell)** — `make` isn't available by default, so run the steps directly:

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt -c constraints.txt
$env:PYTHONPATH = "src"
python data\generate_fake_data.py --num-users 100 --num-sessions 5000 --days 60 --output-dir data\output
python -m analytics.etl                                    # build the DuckDB database
streamlit run dashboard\app.py                             # dashboard at :8501
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000  # (optional) API at :8000
```

## Architecture

```
                 data/generate_fake_data.py   (synthetic, seeded, stdlib-only; model catalog)
                                 │
   data/output/  telemetry_logs.jsonl   openai_usage.jsonl   gemini_usage.jsonl
                 deepseek_usage.csv     employees.csv (shared directory)
                                 │
   ┌─────────────────────────────┴──────────────────────────────────┐
   │ providers/   one adapter per provider: raw format → canonical   │  src/analytics/
   │              rows (claude_code wraps ingest.py's double-nested  │
   │              parser; openai, gemini, deepseek)                  │
   │ etl.py       stream rows into one wide `events` table, load     │
   │              employees, de-dup, write the ingest_runs audit,    │
   │              build the events_enriched view                     │
   └─────────────────────────────┬──────────────────────────────────┘
                                 │
                       data/analytics.duckdb        (single source of truth)
                                 │
                       queries.py   filterable, parameterized SQL → DataFrames
                         ┌───────┴────────┐
            dashboard/app.py          api/main.py
         Streamlit + Plotly,       FastAPI read-only REST v1.1.0
         7 persona-oriented tabs   (same metrics, :8000/docs)
```

**Why these choices**

- **DuckDB** — embedded columnar SQL engine: zero server, one file, fast aggregation
  over the whole dataset. Ideal for a self-contained, reproducible analytics app, and
  it mirrors how this data would be queried in Athena/S3 in production.
- **Streamlit + Plotly** — interactive dashboards in pure Python with one command to
  serve; fast to build and easy for a reviewer to run.
- **Thin, layered Python** — adapters / ETL / queries are separated so each is testable
  in isolation and the query layer is reused verbatim by the dashboard, the API, and the
  tests.

### Architecture decisions

- **Adapter pattern.** Each provider logs usage in its own shape. A `ProviderAdapter`
  in `src/analytics/providers/` maps that shape to canonical rows, and the ETL, queries,
  API and dashboard only ever see canonical rows. Adding a provider means one new
  adapter module plus a `REGISTRY` entry; the core does not change. Every adapter builds
  its rows from a shared `empty_row()`, so all of them emit the same key set.
- **One wide `events` table** with a `provider` column, not a table per provider.
  Every existing query keeps working, and comparing providers is just `GROUP BY provider`.
  Claude Code-only columns (tools, prompts, terminal/CLI/OS) are NULL for other providers.
- **Generated cost.** The generator's model catalog holds illustrative per-1M-token
  prices. It writes `cost_usd` into every record of every format, computed from the
  record's tokens and its model's prices. The ETL never estimates cost. A missing or
  malformed cost stays NULL and is reported as a data-quality finding.
- **Backward compatibility.** The original Claude Code columns, `Filters` fields, query
  signatures, `ingest.iter_events` / `normalize_event` and API routes are unchanged, and
  the original tests pass unmodified. With no provider filter, every metric covers all
  providers. Filtered to `claude_code`, the numbers equal the pre-extension ones.
- **Graceful degradation.** A missing optional provider file is a warning and is marked
  `missing` in `ingest_runs`; only the Claude Code file is mandatory. Malformed lines
  are skipped and counted, never fatal.

### The key data challenges

- **Claude Code** `telemetry_logs.jsonl` is **doubly nested**. Each line is a
  CloudWatch-style *batch*, and the actual events are JSON **strings** inside
  `logEvents[].message`. Every numeric attribute is stored as a string.
- **OpenAI** reports cached tokens *inside* `prompt_tokens` and reasoning tokens
  *inside* `completion_tokens`.
- **Gemini** uses camelCase `usageMetadata` and reports `thoughtsTokenCount` *outside*
  `candidatesTokenCount`.
- **DeepSeek** is a gateway CSV with separate cache-hit and cache-miss columns and no
  session id.

The adapters reconcile these into one token convention. `data/DATASET.md` describes
every raw format, and `.claude/skills/telemetry-analytics/SKILL.md` covers the data
model and query patterns.

## Data model

- **`events`** — one typed row per event for all providers. Event types are
  `api_request`, `api_error`, plus the Claude Code-only `tool_decision`, `tool_result`
  and `user_prompt`. OpenAI `tool_calls` also appear as `tool_result` rows. The
  multi-provider columns are `provider`, `client`, `model_family`, `source_event_id`
  and `reasoning_tokens`.
- **Token convention:** `input_tokens` = uncached prompt tokens, `cache_read_tokens` =
  cached prompt tokens, `output_tokens` = all generated tokens, and `reasoning_tokens`
  is the reasoning subset of the output, never added on top. For Gemini,
  output = candidates + thoughts.
- **`employees`** — directory dimension (`email, full_name, practice, level, location`),
  shared by every provider via `user_email`.
- **`ingest_runs`** — per-provider ETL audit: source file, status, rows read, loaded and
  skipped, duplicates removed, models without a cost.
- **`events_enriched`** — `events` joined to `employees` and the provider labels, plus
  derived columns: date/week/hour, numeric seniority `level_num`, and `total_tokens`
  (NULL-safe sum). Queries build on this view.

## Dashboard

| Tab | Persona | Highlights |
|-----|---------|-----------|
| Overview | Leadership | daily spend, active users, requests (by provider when several are in view), day×hour activity heatmap |
| Providers | Leadership / FinOps / Platform-DX | per-provider cards (spend, users, cost/user, error rate), weekly share of spend/requests/users, cost per 1K tokens, p50/p95 latency, cohort × provider heatmap, multi-provider users + overlap, model leaderboard |
| Cost | FinOps | spend by model, avg cost/request, cost per user by practice & seniority, top spenders |
| Tools | Platform/DX | most-used tools, success rates, rejection rates, authorization source (Claude Code only) |
| Reliability | Platform/DX | API error rate over time, errors by status code & message |
| Sessions & Adoption | All | session duration/cost distributions, Claude Code turns/tool calls, adoption by client / terminal / CLI version / OS / location |
| Anomalies | FinOps / Platform/DX | daily cost & error-rate spikes flagged by a causal rolling z-score (optionally per provider), with tunable window & threshold |

All charts respond to the sidebar filters: provider, date range, practice, seniority
level and model (model families, grouped by provider). Each provider keeps the same
color everywhere. A **Data quality** expander at the bottom shows the ETL audit per
provider.

## Programmatic access (API)

A **FastAPI** service (`AI Usage Analytics API`, v1.1.0) exposes the same metrics as a
read-only JSON API. The dashboard and the API are two thin consumers of one query
layer, so their numbers always agree. The API starts with `docker compose up` on
**http://localhost:8000**; run it alone with `make api`. Interactive Swagger docs are
at **`/docs`**.

| Endpoint | Returns |
|----------|---------|
| `GET /kpis` | headline KPIs (spend, requests, sessions, users, tokens, prompts, errors, providers in use) |
| `GET /activity/daily` · `/activity/heatmap` | daily spend/requests/sessions/users; day-of-week × hour event counts |
| `GET /cost/by-model` · `/cost/by-practice` · `/cost/by-level` | cost breakdowns |
| `GET /users/top?limit=` | top spenders |
| `GET /tools` · `/tools/decision-sources` | tool usage, success/reject, authorization mix (Claude Code only) |
| `GET /errors` · `/errors/rate` | error breakdown & rate over time (`?by_provider=true` for one series per provider) |
| `GET /sessions/summary` | session count, median duration, median/p90/max/total cost (all selected providers); median turns and tool calls over the `cc_sessions` Claude Code sessions in the selection (`null` without Claude Code) |
| `GET /adoption/{dimension}` | adoption by `terminal_type`/`cli_version`/`os_type`/`location`/`client` |
| `GET /anomalies/cost` · `/anomalies/errors` | daily series with anomalies flagged (`?by_provider=true` scores each provider separately) |
| `GET /providers` | known providers, labels, whether the DB has data for each |
| `GET /providers/summary` | per provider: requests, errors, users, cost, tokens, p50/p95 latency, unit costs |
| `GET /providers/share?grain=day\|week\|month` | provider share of cost/requests/users per period |
| `GET /providers/by/{dimension}` | `practice`/`level`/`location`/`client`/`model_family` × provider (400 on other values) |
| `GET /providers/multi-users` | `{distribution, pairs}`: users by number of providers + provider overlap |
| `GET /models/leaderboard?limit=` | per provider × model family: usage, cost, reliability, latency |
| `GET /data-quality` | latest ETL audit per provider (rows read/loaded/skipped, duplicates, unknown models) |
| `GET /filters` · `/health` | filter options; liveness |

Every metric endpoint accepts the dashboard's filters as query params, for example
`?start_date=2026-01-01&end_date=2026-01-31&practices=ML%20Engineering&providers=claude_code`.
The params `practices`, `levels`, `models`, `model_families` and `providers` are
repeatable:

- `models` matches **raw** model ids exactly, so `gpt-4o` does not match
  `gpt-4o-2024-08-06`.
- `model_families` matches the normalized family (`gpt-4o`).
- An unknown `providers` value returns **422** with the allowed values.
- With no `providers` filter, every metric covers all providers.

Example:

```bash
curl "http://localhost:8000/providers/summary"
curl "http://localhost:8000/cost/by-model?providers=openai&model_families=gpt-4o"
curl "http://localhost:8000/anomalies/errors?by_provider=true&window=7&z_thresh=3"
```

## Dataset

`data/generate_fake_data.py` (standard library only, seeded) writes these files to
`data/output/`:

| File | Provider | Format |
|------|----------|--------|
| `telemetry_logs.jsonl` | Claude Code | CloudWatch-style batches of OTel-like events (doubly nested, numbers as strings) |
| `openai_usage.jsonl` | ChatGPT / OpenAI | one Chat Completions-style record per request |
| `gemini_usage.jsonl` | Gemini | one Vertex AI-style record per request (camelCase `usageMetadata`) |
| `deepseek_usage.csv` | DeepSeek | API-gateway CSV, one row per request |
| `employees.csv` | — | employee directory shared by all providers |

Flags: `--num-users` (30), `--num-sessions` (500), `--days` (30), `--seed` (42),
`--output-dir` (`output`), and `--providers` (comma-separated subset of
`claude_code,openai,gemini,deepseek`; empty means all four). `make data` and Docker read
`GEN_NUM_USERS` / `GEN_NUM_SESSIONS` / `GEN_DAYS` / `GEN_SEED` / `GEN_PROVIDERS`
(defaults 100 / 5000 / 60 / 42 / all).

Models, illustrative prices, token and latency profiles, and error rates live in the
generator's `MODEL_CATALOG`. The data has some deliberate features to find: practice-
dependent adoption, a growing Gemini share, a one-day OpenAI 429 storm, a few power
users on every provider, plus replayed and truncated lines to exercise de-duplication
and validation.

For the same arguments, the Claude Code output is byte-identical to the original
Claude Code-only generator (apart from the `uuid4` session/account/org ids, which the
test pins). The other providers use their own seeded RNGs, and
`tests/test_generator.py` checks this. Full format reference: **[data/DATASET.md](data/DATASET.md)**.

## Configuration

Everything is configured through env vars read by `src/analytics/config.py`; see
`.env.example` (no secrets, because none are needed):

- `DATA_DIR`, `TELEMETRY_JSONL`, `EMPLOYEES_CSV`, `DUCKDB_PATH` — file locations.
- `PROVIDERS` — the adapters the ETL loads (default: all four).
- `OPENAI_USAGE_JSONL`, `GEMINI_USAGE_JSONL`, `DEEPSEEK_USAGE_CSV` — provider file
  overrides (default: next to the telemetry file).
- `GEN_*` — dataset generation knobs.

`python -m analytics.etl --providers claude_code,openai` loads a subset for one run.

## Commands

| Command | Description |
|---------|-------------|
| `make install` | Install runtime + test deps, pinned by `constraints.txt` |
| `make data` | Generate synthetic data for `GEN_PROVIDERS` into `data/output/` |
| `make etl` | Build `data/analytics.duckdb` |
| `make pipeline` | `data` + `etl` |
| `make dashboard` | Serve the Streamlit dashboard |
| `make api` | Serve the FastAPI server (docs at `:8000/docs`) |
| `make test` | Run the test suite |
| `make clean` | Remove generated data and database |
| `make docker` | `docker compose up --build` |

For ad-hoc SQL, run `duckdb data/analytics.duckdb`, or query through
`analytics.queries.connect()`.

## Testing & validation

`make test` runs these suites:

- **`test_ingest.py`** — Claude Code double-nesting unpack, type casting, required-field
  validation, malformed-input skipping, an ETL→query round-trip and the anomaly detector.
- **`test_providers.py`** — each adapter's token mapping, error rows, bad lines and
  model normalization.
- **`test_etl_multi.py`** — the four-provider ETL: de-duplication, missing optional
  files, NULL-safe totals, and Claude Code numbers unchanged under the provider filter.
- **`test_generator.py`** — determinism and the byte-identical Claude Code output.
- **`test_api.py`** — every endpoint via `TestClient`, including 422 on an unknown
  provider.

At runtime the ETL validates input: bad lines and events are counted and reported per
provider in `ingest_runs`, and a missing required file raises an actionable error.

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request in two gated stages:

1. **`test`** — installs `requirements-dev.txt` with `-c constraints.txt` on Python 3.12,
   using full git history for the generator test, and runs the pytest suite.
2. **`docker`** — runs only if the tests pass. It runs `docker compose build`, then
   brings the whole stack up with `docker compose up` on a small dataset for speed. It
   waits for the dashboard *and* API health checks, smoke-tests real `/kpis`,
   `/cost/by-model` and `/providers/summary` responses (one row per provider in
   `GEN_PROVIDERS`), and then tears the stack down.

Both jobs run on the pinned `ubuntu-26.04` image, not `ubuntu-latest`. The Docker image
is a **slim multi-stage build** on a digest-pinned `python:3.12.x-slim-trixie`:

- It contains runtime deps only (`requirements.txt` + `constraints.txt`); test tools
  live in `requirements-dev.txt`.
- There is no pip and no build tooling in the final image.
- The app runs as an unprivileged user. `docker/run-as-data-owner.sh` adopts the owner
  of the `./data` mount, falls back to `app`, and never chowns a bind mount recursively.

A green pipeline therefore proves both that the code is correct and that the
single-command stack actually boots end-to-end.

This is **CI only — there is no CD (no delivery/deployment)**. The stack is brought up
*inside the ephemeral CI runner* purely to smoke-test it, then torn down; nothing is
published or exposed at a reachable URL. To actually use the app, run `docker compose up`
yourself and open the dashboard at `localhost:8501` and the API at `localhost:8000`.

## Agent setup (reproducible, committed)

This solution was built with **Claude Code**, tuned for the task via committed files:

- **`CLAUDE.md`** — repo guide: the per-provider format traps, the canonical schema and
  token mapping, the generator guarantees, and the test task's hard constraints.
- **`.claude/skills/telemetry-analytics/`** — a custom skill that teaches the agent the
  data model, the query helpers (including the provider-comparison functions and
  `Filters`), SQL patterns, extension conventions and persona framing. With it the agent
  can analyze the dataset and safely add metrics and charts.
- **`.claude/agents/`** — three project subagents with strict ownership boundaries:
  - `backend-engineer` owns the pipeline, API, tests, generator, Docker and CI.
  - `frontend-engineer` owns `dashboard/` only.
  - `code-reviewer` is read-only.

  Each phase of work lands as one PR into `main` after reviewer approval and green CI.

No API keys are required (synthetic local data only). Configuration is via env vars;
see `.env.example`. **[LLM_USAGE.md](LLM_USAGE.md)** has the full write-up: what the
agent generated, how the subagents were used, what the reviews caught, and how to
reproduce the setup.

## Project layout

```
src/analytics/          ingest.py · etl.py · queries.py · config.py   (pipeline + query API)
src/analytics/providers/  base.py (contract, canonical schema) · models.py (model_family)
                        claude_code.py · openai.py · gemini.py · deepseek.py   (adapters)
dashboard/app.py        Streamlit dashboard
api/main.py             FastAPI read-only REST layer
data/                   generate_fake_data.py · DATASET.md · output/ (generated)
tests/                  test_ingest · test_providers · test_etl_multi · test_generator · test_api
docker/                 entrypoint.sh (generate → ETL → dashboard) · run-as-data-owner.sh (privilege drop)
Dockerfile              slim multi-stage image
requirements.txt        runtime deps (ranges) · requirements-dev.txt  + pytest/httpx
constraints.txt         exact pins verified in the image; used by Docker, CI and `make install`
.github/workflows/      ci.yml (tests → build → compose smoke test)
.claude/skills/         telemetry-analytics skill
.claude/agents/         backend-engineer · frontend-engineer · code-reviewer
ANALYTICS.md            metric definitions + headline findings (companion to the queries)
LLM_USAGE.md            agent setup, subagent workflow, reproduction steps
```

## Findings

**[ANALYTICS.md](ANALYTICS.md)** has the metric definitions (tied to `queries.py`), the
headline Claude Code findings, and the provider comparison, with the numbers behind
them. All figures are computed from `data/analytics.duckdb` on the default dataset.

## Optional enhancements

- **Anomaly detection (implemented)** — the *Anomalies* tab flags daily cost and
  error-rate spikes with a causal rolling z-score (`queries.cost_anomalies` /
  `error_rate_anomalies`, unit-tested). It can score each provider separately, and the
  window and threshold are tunable in the UI.
- **API endpoints (implemented)** — the FastAPI service above gives programmatic,
  filterable access to every metric.
- **Multi-provider analytics (implemented)** — four providers behind one adapter
  interface. The same interface could later host live usage connectors, but none are
  included: this project is synthetic data only.

Further ideas: incremental ingest for streaming; per-user cohort retention; forecasting.
