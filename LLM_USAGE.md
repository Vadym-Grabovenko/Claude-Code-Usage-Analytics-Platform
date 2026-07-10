# LLM Usage & Agent Setup

Documentation of how this solution was built with an AI agent, which components the
agent generated, and how to reproduce the tuned setup — the test task's
*"Agent Setup & Tuning"* and *"Documentation of LLM usage"* deliverables.

> **Ownership.** The agent produced the code; every line is understood and owned by
> the author. The architecture, the trade-offs, and the problems solved (below) can be
> explained without reference to the tool that typed them.

---

## The tool

- **Agent:** [Claude Code](https://claude.com/claude-code) — Anthropic's CLI coding
  agent — driven by a Claude Opus model.
- **Why an agentic CLI (not web chat):** the task spans many files, a real dataset,
  Docker, and iterative test/run loops. A repo-aware agent that can read files, run the
  pipeline, execute `pytest`, and inspect the DuckDB build closes the loop that plain
  copy-paste chat cannot — and the test task explicitly excludes plain chat usage.

The tuning is **committed and reproducible**: it lives entirely in two files in the
repo, both auto-loaded by Claude Code with no manual setup.

---

## The committed, reproducible setup

| Component | Path | Role |
|-----------|------|------|
| **Project instructions** | [`CLAUDE.md`](CLAUDE.md) | Repo-level rules file, auto-loaded on every session. Encodes the dataset's *double-nesting trap*, the full event schema, the employee join, and the test task's hard requirements. |
| **Custom skill** | [`.claude/skills/telemetry-analytics/SKILL.md`](.claude/skills/telemetry-analytics/SKILL.md) | Model-invoked skill, auto-discovered under `.claude/skills/`. Teaches the agent the data model, the query helpers, SQL patterns, and the conventions for safely extending metrics/charts. |

No secrets, MCP servers, hooks, or external services are used — the solution runs on
synthetic local data only. Configuration is via environment variables; see
[`.env.example`](.env.example).

### Why this tuning actually helped

This is the point of the "AI-assisted development" grading criterion — the setup
measurably improved the agent's output:

- **The data trap is stated once, up front.** `telemetry_logs.jsonl` is doubly nested
  (each line is a CloudWatch batch; real events are JSON *strings* inside
  `logEvents[].message`) and every numeric attribute is a string. Encoding this in
  `CLAUDE.md` meant the agent wrote a correct streaming parser on the first pass instead
  of discovering the nesting by trial and error.
- **The schema and join are pinned.** Event types, their distinguishing attributes, and
  the `attributes["user.email"] ↔ employees.email` join (with the blank top-level email
  gotcha) are documented, so queries were written against the right columns immediately.
- **The skill enforces consistency.** New metrics follow one shape — `(con, f: Filters)`
  over `events_enriched`, `{where}` placeholder, **bind params only**, return a
  DataFrame — and column names are allow-listed. The agent extended the query layer
  safely (no SQL injection, filters apply everywhere) because the pattern was written
  down, not re-invented per function.
- **Personas are baked in.** The skill frames every insight to a decision (routing,
  enablement, guardrails) for Leadership / FinOps / Platform-DX, which shaped both the
  dashboard tabs and the findings.

---

## What the agent generated

Effectively the entire solution was agent-produced, then reviewed and verified:

- **Pipeline** — `src/analytics/ingest.py`, `etl.py`, `queries.py`, `config.py`
- **Dashboard** — `dashboard/app.py` (Streamlit + Plotly, 6 persona tabs)
- **API** — `api/main.py` (FastAPI read-only REST layer over the query functions)
- **Orchestration** — `Dockerfile`, `docker-compose.yml`, `docker/entrypoint.sh`, `Makefile`
- **Tests** — `tests/test_ingest.py` (parsing, casting, validation, ETL→query round-trip)
- **Docs** — `README.md`, `ANALYTICS.md`, this file, and the agent config itself
- **Presentation** — `presentation.pptx` (generated with
  `python-pptx`), with all figures pulled live from `data/analytics.duckdb`

The only file **not** written for this solution is `data/generate_fake_data.py` — the
dataset generator provided with the test task.

## Human direction & oversight

- Set the architecture direction (DuckDB single-file store, thin layered pipeline,
  Streamlit dashboard, persona framing) and made the build/tooling calls
  (Docker vs. Make, no shell-script wrappers).
- Reviewed every change and verified the result end-to-end: full `docker compose up`
  build (image → data generation → DuckDB → Streamlit health on `:8501`), the local
  `make` path, and `pytest` (all passing).
- Findings in the deck/`ANALYTICS.md` were computed from the actual database, not
  asserted — the numbers are reproducible via `queries.py`.

## Problems solved during development (worth being able to explain)

- **Double-nested parsing** — two `json.loads` layers per line plus string→typed casts;
  malformed batches/events are skipped and *counted* (`IngestStats`) rather than
  crashing the run.
- **Bounded memory on a large file** — the 546 MB JSONL is streamed and inserted into
  DuckDB in 50k-row chunks, so peak memory stays flat.
- **Docker engine vs. CLI** — the dev machine had only the Docker CLI; a lightweight
  engine (colima) + Compose v2 was needed to actually run `docker compose up`. This is
  documented in the README prerequisites.
- **Reading the DB to build the deck** — the host Python had no `duckdb`; figures were
  pulled through an isolated virtualenv so the presentation numbers match the dashboard.

---

## Reproduce the agent setup

The setup requires no installation — it *is* the two committed files:

1. Install Claude Code and open this repository:
   ```bash
   npm install -g @anthropic-ai/claude-code   # or see claude.com/claude-code
   cd "Test task" && claude
   ```
2. `CLAUDE.md` (repo root) loads automatically; the `telemetry-analytics` skill is
   auto-discovered under `.claude/skills/` and triggers on any task touching the
   dataset, the DuckDB database, the pipeline, or the dashboard.
3. Verify the tuning is live — ask the agent, e.g. *"add a metric for cache-hit ratio by
   model"* — and it will follow the query conventions in the skill (bind params,
   `events_enriched`, `Filters`) without being told.

Any other agentic tool can be pointed at the same knowledge: the equivalent rules file
(`AGENTS.md`, `GEMINI.md`, `.cursorrules`, …) would carry the contents of `CLAUDE.md`,
and the skill body is plain Markdown documenting the data model and query patterns.
