# LLM Usage & Agent Setup

This file documents how the solution was built with an AI agent, which components the
agent generated, and how to reproduce the tuned setup. It covers the test task's
*"Agent Setup & Tuning"* and *"Documentation of LLM usage"* deliverables.

> **Ownership.** The agent produced the code; every line is understood and owned by
> the author. The architecture, the trade-offs, and the problems solved (below) can be
> explained without reference to the tool that typed them.

---

## The tool

- **Agent:** [Claude Code](https://claude.com/claude-code), Anthropic's CLI coding
  agent, driven by a Claude Opus model.
- **Why an agentic CLI (not web chat):** the task spans many files, a real dataset,
  Docker, and iterative test/run loops. A repo-aware agent can read files, run the
  pipeline, execute `pytest`, and inspect the DuckDB build, which closes the loop that
  copy-paste chat cannot. The test task also explicitly excludes plain chat usage.

The tuning is **committed and reproducible**. It lives entirely in files in the repo,
and Claude Code loads all of them automatically with no manual setup.

---

## The committed, reproducible setup

| Component | Path | Role |
|-----------|------|------|
| **Project instructions** | [`CLAUDE.md`](CLAUDE.md) | Repo-level rules file, auto-loaded on every session. Covers each provider's raw-format traps (Claude Code's *double nesting* and string numerics, OpenAI/Gemini/DeepSeek token semantics), the canonical schema and token mapping, the generator's guarantees, the employee join, the subagent ownership table, and the test task's hard requirements. |
| **Custom skill** | [`.claude/skills/telemetry-analytics/SKILL.md`](.claude/skills/telemetry-analytics/SKILL.md) | Model-invoked skill, auto-discovered under `.claude/skills/`. Teaches the agent the data model, `Filters` (`providers`, `models` = raw ids, `model_families` = families), the query helpers including the provider-comparison functions, which metrics are Claude Code only, SQL patterns, extension conventions and persona framing. |
| **Subagents** | [`.claude/agents/`](.claude/agents/) | Three project subagents with their own system prompts, tool lists and file-ownership boundaries: `backend-engineer`, `frontend-engineer`, `code-reviewer`. |

No secrets, MCP servers, hooks, or external services are used, because the solution
runs on synthetic local data only. Configuration is via environment variables; see
[`.env.example`](.env.example).

### Why this tuning actually helped

This is the point of the "AI-assisted development" grading criterion: the setup
measurably improved the agent's output.

- **The data traps are stated once, up front.** `telemetry_logs.jsonl` is doubly nested
  (each line is a CloudWatch batch; real events are JSON *strings* inside
  `logEvents[].message`), and every numeric attribute is a string. With this in
  `CLAUDE.md`, the agent wrote a correct streaming parser on the first pass instead of
  discovering the nesting by trial and error. The same applies to the newer formats:
  OpenAI's cached tokens sit inside `prompt_tokens`, and Gemini's thoughts sit outside
  `candidatesTokenCount`.
- **The schema and join are pinned.** The event types, the canonical columns, and the
  `user_email ↔ employees.email` join (including the blank top-level email gotcha) are
  documented, so queries were written against the right columns immediately.
- **The skill enforces consistency.** New metrics follow one shape: `(con, f: Filters)`
  over `events_enriched`, the `{where}` placeholder, **bind params only**, and a
  DataFrame as the result. Column names are allow-listed. Because the pattern was
  written down, the provider-comparison functions came out in the same safe shape as
  the original ones.
- **Personas are baked in.** The skill frames every insight to a decision (routing,
  consolidation, guardrails) for Leadership / FinOps / Platform-DX, which shaped both
  the dashboard tabs and the findings.

---

## What the agent generated

Effectively the entire solution was agent-produced, then reviewed and verified:

- **Pipeline** — `src/analytics/ingest.py`, `etl.py`, `queries.py`, `config.py`, and the
  provider adapters in `src/analytics/providers/`
- **Generator extension** — the model catalog and the OpenAI/Gemini/DeepSeek formats in
  `data/generate_fake_data.py`, added on top of the provided Claude Code generator.
  Claude Code output is unchanged and test-enforced.
- **Dashboard** — `dashboard/app.py` (Streamlit + Plotly, 7 persona tabs incl. Providers)
- **API** — `api/main.py` (FastAPI read-only REST layer over the query functions, v1.1.0)
- **Orchestration** — `Dockerfile`, `docker-compose.yml`, `docker/entrypoint.sh`,
  `docker/run-as-data-owner.sh`, `Makefile`, `constraints.txt`, `.github/workflows/ci.yml`
- **Tests** — `tests/test_ingest.py`, `test_providers.py`, `test_etl_multi.py`,
  `test_generator.py`, `test_api.py`
- **Docs** — `README.md`, `ANALYTICS.md`, `data/DATASET.md`, this file, and the agent
  config itself

The only file the agent did not write from scratch is `data/generate_fake_data.py`,
the dataset generator provided with the test task. The agent extended it under a
byte-identical-output constraint.

A slide deck was generated in an earlier iteration and later removed from the repo.
`ANALYTICS.md` is now the single written home of the findings.

## Human direction & oversight

- Set the architecture direction (DuckDB single-file store, thin layered pipeline,
  adapter pattern, Streamlit dashboard, persona framing) and made the build/tooling
  calls (Docker vs. Make, no shell-script wrappers).
- Decided the multi-provider scope. Each provider keeps its own raw format. The
  existing generator is extended rather than replaced, and it writes the cost into every
  record, so the ETL never estimates cost. Data is synthetic only, with no real
  connectors.
- Reviewed every change and verified the result end-to-end: full `docker compose up`
  build (image → data generation → DuckDB → Streamlit health on `:8501`), the local
  `make` path, and `pytest` (all passing).
- Findings in `ANALYTICS.md` were computed from the actual database, not asserted. The
  numbers are reproducible via `queries.py`.

---

## The multi-provider extension: planned up front, three subagents, one PR per phase

The extension from Claude Code-only analytics to four providers was run as a small
team of agents rather than one long chat.

**1. Plan first.** The scope, the data contract and a phase plan were fixed up front, in
a local planning document (not committed), before any code was written. Each phase was
then implemented by the subagents, reviewed, and merged as its own PR.

**2. Three subagents with hard boundaries** (`.claude/agents/`):

| Agent | Used for | Boundary |
|---|---|---|
| `backend-engineer` | generator catalog, adapters, ETL, query functions, API, Docker/CI, tests, these docs | never edits `dashboard/` |
| `frontend-engineer` | Provider filter, Providers tab, provider colouring, CC-only notices, data-quality expander | only `dashboard/`; no SQL, asks the backend for query functions |
| `code-reviewer` | reviewing each diff before merge | read-only (Read/Grep/Glob/Bash for checks), reports BLOCKER/MAJOR/MINOR/NIT + verdict |

The backend shipped the **contract first**: canonical columns, `Filters.providers`, and
the new query signatures. Backend and frontend then worked **in parallel** against that
contract, and the ownership split meant they never edited the same file.

**3. One PR per phase into `main`.** Each phase merged only after `code-reviewer`
approval and green CI:

- Phase 0–2: schema, generator, adapters, ETL and dashboard.
- Phase 3: API, CI and the slim image.
- Then cleanup and documentation.

**What the review rounds caught** (each fixed before merge):

- **A recursive `chown` on the data mount.** The first non-root image fixed permissions
  on `/app/data` recursively. On Linux that directory is the repo's own `./data` bind
  mount, so the image would have taken the user's files away from them. Now
  `docker/run-as-data-owner.sh` adopts the owner of the mount and never chowns a bind
  mount. It only chowns an *empty* directory, non-recursively. If root-owned leftovers
  from an older image are present, it stops and prints the one-line host fix.
- **A Linux-only CI failure in the generator test.** The byte-identical Claude Code check
  compared against digests recorded on macOS. The original generator writes
  `str(random.gauss(...))`, and libm differs in the last float digit between macOS and
  Linux, so the hashes failed on the CI runner. The fix has two parts. The test re-runs
  the original generator (from git history) on the same machine and compares bytes, and
  the frozen fallback digests are float-normalised. CI checks out full history for this.
- **A regression in the Sessions tab.** Once other providers were in the data, the median
  turns and tool calls per session collapsed toward 0. Those providers have sessions but
  no `user_prompt` or tool events. The tab now computes these medians on the Claude Code
  slice only, and shows an `st.info` when Claude Code isn't selected.
- Also caught along the way:
  - A `total_tokens` sum that went NULL when any term was NULL; it is now `COALESCE` per
    term.
  - The `models` filter silently matching families, so `gpt-4o` swallowed dated
    snapshots. It is now split into `models` (raw ids) and `model_families`.

---

## Problems solved during development (worth being able to explain)

- **Double-nested parsing.** Each line needs two `json.loads` layers plus string→typed
  casts. Malformed batches/events are skipped and *counted* (`IngestStats`, and
  `ingest_runs` per provider) rather than crashing the run.
- **Four formats, one token convention.** The adapters agree that input means uncached
  input and that reasoning is a subset of output. For Gemini, that requires folding
  `thoughtsTokenCount` into output. Without this, cost per token and totals would not
  be comparable across providers.
- **Bounded memory on a large file.** The ~520 MB Claude Code JSONL is streamed and
  inserted into DuckDB in 50k-row chunks against a pre-declared schema, so peak memory
  stays flat.
- **Docker engine vs. CLI.** The dev machine had only the Docker CLI. A lightweight
  engine (colima) plus Compose v2 was needed to actually run `docker compose up`. The
  README prerequisites document this.
- **Reading the DB from the host.** The host Python had no `duckdb`, so figures were
  pulled through an isolated virtualenv. That way the `ANALYTICS.md` numbers match the
  dashboard.

---

## Reproduce the agent setup

The setup requires no installation; it *is* the committed files:

1. Install Claude Code and open this repository:
   ```bash
   npm install -g @anthropic-ai/claude-code   # or see claude.com/claude-code
   cd "Test task" && claude
   ```
2. `CLAUDE.md` (repo root) loads automatically. The `telemetry-analytics` skill is
   auto-discovered under `.claude/skills/` and triggers on any task touching the
   dataset, the DuckDB database, the pipeline, the API or the dashboard.
3. The subagents in `.claude/agents/` are picked up automatically; list them with
   `/agents`. Invoke them by name, for example:
   ```
   > Use the backend-engineer agent to add a cache-hit-ratio-by-provider query and endpoint
   > Use the frontend-engineer agent to chart it on the Providers tab
   > Use the code-reviewer agent to review the current diff
   ```
4. Follow the same loop: contract first, then backend and frontend in parallel, then
   `code-reviewer` until it returns `APPROVE`. Open one PR per phase into `main`, and
   merge it only when CI is green.
5. Verify the tuning is live by asking, for example, *"add a metric for cache-hit ratio
   by model"*. The agent will follow the query conventions in the skill (bind params,
   `events_enriched`, `Filters`, CC-only caveats) without being told.

Any other agentic tool can be pointed at the same knowledge. The equivalent rules file
(`AGENTS.md`, `GEMINI.md`, `.cursorrules`, …) would carry the contents of `CLAUDE.md`,
and the skill and agent bodies are plain Markdown.
