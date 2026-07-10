# Analytics Reference

The metrics, definitions, and headline findings behind the dashboard and the
[presentation](presentation.pptx). Every number here is computed from
`data/analytics.duckdb` by the functions in `src/analytics/queries.py` — this doc
is the written companion to those queries, not a separate calculation.

- **Dataset window:** 2025-12-03 → 2026-01-31 (60 days)
- **Scale:** 100 users · 5,000 sessions · 454,428 events · 118,014 API requests
- **Default build:** `GEN_NUM_USERS=100 GEN_NUM_SESSIONS=5000 GEN_DAYS=60 GEN_SEED=42`
  (seeded — the figures below reproduce exactly)

All queries run over the `events_enriched` view (`events` LEFT JOIN `employees`,
plus derived `event_date/week/hour/dow`, `level_num`, `total_tokens`). Type-specific
columns are only populated on the relevant `event_type`, so **filter by `event_type`
before aggregating** (e.g. only `api_request` rows carry `cost_usd`).

---

## Metric definitions

| Metric | Definition | Source query |
|--------|-----------|--------------|
| **Total spend** | `sum(cost_usd)` over `api_request` rows (other types are NULL, so a global sum is safe) | `kpis` |
| **API requests** | `count(*) WHERE event_type='api_request'` | `kpis` |
| **Active users** | `count(DISTINCT user_email)` | `kpis` |
| **Total tokens** | `sum(input+output+cache_read+cache_creation)` on `api_request` rows | `kpis` |
| **API error rate** | `api_error` count ÷ `api_request` count | `kpis`, `error_rate_over_time` |
| **Cost per user (cohort)** | `sum(cost_usd) / count(DISTINCT user_email)` grouped by practice or level | `cost_by_practice`, `cost_by_level` |
| **Avg cost per request** | `avg(cost_usd)` per model on `api_request` rows | `cost_by_model` |
| **Tool success rate** | `avg(success::INT)` over `tool_result` rows per tool | `tool_usage` |
| **Tool reject rate** | `tool_decision` rows with `decision='reject'` ÷ all decisions, per tool | `tool_usage` |
| **Decision source mix** | share of `tool_decision` rows by `source` (config / user_*) | `decision_sources` |
| **Session** | rows sharing `session_id`; duration = `max(ts) − min(ts)`; turns = count of `user_prompt`; tool calls = count of `tool_result` | `session_stats` |
| **Adoption by dimension** | `count(DISTINCT user_email)` grouped by an allow-listed env column | `adoption_by_dimension` |
| **Cost / error anomaly** | causal rolling z-score: `(day − trailing_mean) / trailing_std` over the previous N days; flagged when `\|z\| ≥ threshold` | `cost_anomalies`, `error_rate_anomalies`, `detect_anomalies` |

Seniority (`level`) is always ordered by the numeric `level_num` (L10 > L2), never the
`L#` string.

---

## Headline findings

### 1. Usage is overwhelmingly tool-driven
Event mix: `tool_decision` 33.3% · `tool_result` 32.7% · `api_request` 26.0% ·
`user_prompt` 7.7% · `api_error` 0.3%. **~66% of all events are tool interactions.**
Each prompt drives ~4 model turns and ~16 tool calls — the value is in the agentic
loop, not one-shot completion.

### 2. 71% of spend sits in one model family — routing is the lever
| Family | Calls | Share of calls | Cost | Share of cost | $/request |
|--------|------:|-----:|------:|-----:|-----:|
| Opus   | 49,652 | 42% | $4,258 | **71%** | $0.086 |
| Sonnet | 22,383 | 19% | $1,558 | 26% | $0.070 |
| Haiku  | 45,979 | 39% | $186 | 3% | $0.004 |

Opus costs **~21× more per request than Haiku**. Haiku already absorbs 39% of calls
for 3% of spend, so steering low-complexity turns off Opus is the biggest cost lever.

### 3. Spend is flat across seniority and practice — broad adoption
Cost per user stays in a tight **$51–70** band across all ten levels (L1–L10) and
across every practice ($53–62). Neither seniority nor practice predicts spend →
uniform, healthy adoption rather than a few power users; enablement/budgeting can be
flat per head.

### 4. Bash is the reliability & latency hotspot
Among common tools, Bash is the outlier: **93.0% success** (vs 98–99% elsewhere) and
**5,363 ms** average latency. It's the 2nd-most-used tool (42,332 runs) → the clear
target for sandboxing / better error surfacing. (`Task` averages ~500 s because it
runs a whole sub-agent — exclude it from interactive-latency SLOs.)

### 5. Config carries authorization; friction is low
Decision sources: **config 80%** · user_temporary 15% · user_permanent 3% ·
user_reject 2%. Only **2% of tool calls are actively rejected** — trust is high. The
15% temporary approvals are the backlog worth promoting to permanent allowlists.

### 6. Among typed errors, rate-limiting leads
1,362 errors total. After the `undefined` bucket (774), **429 rate-limit (299)**
dominates, ahead of 400 (172), 500 (83), 401 (34). The top named failure is
capacity/throttling, not user error → retry/backoff and quota headroom matter most.

### 7. Sessions are short, cheap, and agentic — with a heavy tail
Median session: **7 min · 4 turns · 16 tool calls · $0.67**. p90 cost $2.84,
max $18.44. Typical use is a quick sub-dollar task; the long tail of deep sessions is
where to watch for runaway cost.

### 8. IDE-embedded and Mac-first
Terminals: VS Code 39 users, JetBrains family 37 (PyCharm/WebStorm/IntelliJ combined),
Warp 12 → IDE integrations dominate standalone terminals. OS: **macOS 81 / Linux 18 /
Windows 1**. CLI versions span 2.1.37 → 2.1.56 with no forced upgrade (fragmentation
signal).

---

## Anomaly detection (bonus)

The *Anomalies* dashboard tab flags days where daily **spend** or **API error rate**
departs from recent behaviour. Method — a **causal rolling z-score**: each day is
scored against the mean ± standard deviation of the *previous* N days only (the current
day is excluded via a shift), so a spike is measured against the normal that preceded it
rather than smoothing itself away. A day is anomalous when `|z| ≥ threshold`; both the
window (default 7 days) and the threshold (default 3.0) are tunable in the UI.

On the reference dataset this flags **2 cost-spike days and 2 error-rate-spike days** at
z ≥ 3 (max |z| ≈ 4.0 for cost, 5.2 for error rate). The detector is a pure function
(`detect_anomalies`) and is unit-tested against a synthetic spike and a flat series.

## Persona framing

| Persona | Dashboard tab | Question answered |
|---------|--------------|-------------------|
| Leadership | Overview | Is adoption growing, and what does it cost? |
| FinOps | Cost | Where is the routing/spend lever? |
| Platform / DX | Tools + Reliability | Which tools need guardrails? |

Insights are framed to a decision (model routing, enablement, guardrails), not just a
number — the grading criterion "insights that inform product decisions."

---

## Reproduce these numbers

```bash
make pipeline          # generate data + build data/analytics.duckdb
make dashboard         # explore interactively at http://localhost:8501
```

Ad-hoc, via the same query layer the dashboard uses:

```python
import sys; sys.path.insert(0, "src")
from analytics import queries as q
from analytics.queries import Filters
con = q.connect()                       # read-only DuckDB connection
q.kpis(con, Filters())                  # headline KPIs
q.cost_by_model(con, Filters())         # the model-family table above
q.tool_usage(con, Filters())            # success / reject / latency per tool
```

Or open the database directly: `duckdb data/analytics.duckdb`. See
`.claude/skills/telemetry-analytics/SKILL.md` for the full data model and query
patterns.
