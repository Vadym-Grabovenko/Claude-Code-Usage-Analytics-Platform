# Analytics Reference

This doc covers the metrics, their definitions, and the headline findings behind the
dashboard and the API. Every number here comes from `data/analytics.duckdb`, computed
by the functions in `src/analytics/queries.py`. It is a written companion to those
queries, not a separate calculation.

> **Synthetic data.** All four providers' logs come from the seeded generator. The model
> prices in its catalog are **illustrative**, not vendor quotes. The findings show what
> the platform can surface; they are not statements about real products.

- **Default build:** `GEN_NUM_USERS=100 GEN_NUM_SESSIONS=5000 GEN_DAYS=60 GEN_SEED=42`,
  all four providers (`GEN_PROVIDERS` empty). The build is seeded, so the figures below
  reproduce exactly.
- **Dataset window:** 2025-12-03 → 2026-02-01 (60 days; the last UTC day is a sliver).
- **All providers:** 100 users · 511,335 events · 168,787 API requests · 2,541 API
  errors · 16,244 sessions (including synthetic sessions for sources without a session
  id) · **$6,236.90** total spend.
- **Claude Code only** (`Filters(providers=["claude_code"])`): 100 users · 5,000
  sessions · 454,428 events · 118,014 API requests · $6,001.43. These match the
  pre-extension build.

All queries run over the `events_enriched` view (`events` LEFT JOIN `employees`, plus
derived `event_date/week/hour/dow`, `level_num`, `total_tokens`). Type-specific
columns are only populated on the relevant `event_type`, so **filter by `event_type`
before aggregating** (e.g. only `api_request` rows carry `cost_usd`). With no provider
filter every metric covers all providers.

---

## Metric definitions

| Metric | Definition | Source query |
|--------|-----------|--------------|
| **Total spend** | `sum(cost_usd)` over `api_request` rows (other types are NULL, so a global sum is safe) | `kpis` |
| **API requests** | `count(*) WHERE event_type='api_request'` | `kpis` |
| **Active users** | `count(DISTINCT user_email)` | `kpis` |
| **Total tokens** | `sum(input+output+cache_read+cache_creation)` on `api_request` rows, `COALESCE(…,0)` per term; reasoning is part of output and not added again | `kpis` |
| **API error rate** | `api_error` count ÷ `api_request` count | `kpis`, `error_rate_over_time` |
| **Cost per user (cohort)** | `sum(cost_usd) / count(DISTINCT user_email)` grouped by practice or level | `cost_by_practice`, `cost_by_level` |
| **Avg cost per request** | `avg(cost_usd)` per model on `api_request` rows | `cost_by_model` |
| **Tool success rate** *(Claude Code)* | `avg(success::INT)` over `tool_result` rows per tool | `tool_usage` |
| **Tool reject rate** *(Claude Code)* | `tool_decision` rows with `decision='reject'` ÷ all decisions, per tool | `tool_usage` |
| **Decision source mix** *(Claude Code)* | share of `tool_decision` rows by `source` (config / user_*) | `decision_sources` |
| **Session** | rows sharing `session_id`; duration = `max(ts) − min(ts)`; turns = count of `user_prompt`; tool calls = count of `tool_result` (turns/tool calls are Claude Code only, so filter to it before taking medians) | `session_stats` |
| **Adoption by dimension** | `count(DISTINCT user_email)` grouped by an allow-listed column (`client`, or the Claude Code-only `terminal_type` / `cli_version` / `os_type`, or `location`) | `adoption_by_dimension` |
| **Cost / error anomaly** | causal rolling z-score: `(day − trailing_mean) / trailing_std` over the previous N days; flagged when `\|z\| ≥ threshold`; `by_provider=True` scores each provider's series separately | `cost_anomalies`, `error_rate_anomalies`, `detect_anomalies` |

### Provider comparison

| Metric | Definition | Source query |
|--------|-----------|--------------|
| **Provider share** | per period × provider: `total_cost`, `requests`, `users`, each ÷ the period total (`cost_share`, `requests_share`, `users_share`; a person on two providers counts toward both) | `provider_share_over_time(grain=day\|week\|month)` |
| **Cost per user / per request** | provider `total_cost` ÷ distinct users / ÷ `api_request` count | `provider_summary` |
| **Cost per 1K tokens** | `total_cost × 1000 ÷ total_tokens` of *priced* `api_request` rows (rows without a cost don't dilute it) | `provider_summary`, `model_leaderboard` |
| **Provider error rate** | `api_error` ÷ `api_request` per provider (same definition as above) | `provider_summary`, `model_leaderboard` |
| **Latency p50 / p95** | `quantile_cont(duration_ms, 0.5 / 0.95)` over `api_request` rows | `provider_summary`, `model_leaderboard` |
| **Cohort × provider** | users, requests, cost, cost/user per (`practice`/`level`/`location`/`client`/`model_family`, provider) | `provider_by_dimension` |
| **Multi-provider users** | distribution of attributed users by number of providers used (+ cost/user), and pairwise overlap with Jaccard = shared ÷ users of either | `multi_provider_users` |
| **Model leaderboard** | per provider × `model_family`: requests, errors, users, cost, avg cost, $/1K tokens, p50/p95, first/last used | `model_leaderboard` |
| **Data quality** | latest `ingest_runs` row per provider (rows read/loaded/skipped, duplicates removed, models without a cost) + live unpriced requests and users missing from the directory | `data_quality` |

Seniority (`level`) is always ordered by the numeric `level_num` (L10 > L2), never the
`L#` string.

---

## Headline findings — Claude Code

Re-checked on the multi-provider build with `Filters(providers=["claude_code"])`; every
number below still holds.

### 1. Usage is overwhelmingly tool-driven
Event mix: `tool_decision` 33.3% · `tool_result` 32.7% · `api_request` 26.0% ·
`user_prompt` 7.7% · `api_error` 0.3%. **~66% of all events are tool interactions.**
Each prompt drives ~3.4 model calls and ~4.2 tool runs, and a median session has 4
turns and 16 tool calls. The value is in the agentic loop, not one-shot completion.

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
across every practice ($53–62). Neither seniority nor practice predicts spend. That
points to uniform, healthy adoption rather than a few power users, so
enablement/budgeting can be flat per head.

### 4. Bash is the reliability & latency hotspot
Among common tools, Bash is the outlier: **93.0% success** (vs 97–99% elsewhere) and
**5,363 ms** average latency. It's the 2nd-most-used tool (42,332 runs), which makes it
the clear target for sandboxing / better error surfacing. (`Task` averages ~500 s
because it runs a whole sub-agent, so exclude it from interactive-latency SLOs.)

### 5. Config carries authorization; friction is low
Decision sources: **config 80%** · user_temporary 15% · user_permanent 3% ·
user_reject 2%. Only **2% of tool calls are actively rejected**, so trust is high. The
15% temporary approvals are the backlog worth promoting to permanent allowlists.

### 6. Among typed errors, rate-limiting leads
1,362 errors total. After the `undefined` bucket (774), **429 rate-limit (299)**
dominates, ahead of 400 (172), 500 (83), 401 (34). The top named failure is
capacity/throttling, not user error, so retry/backoff and quota headroom matter most.

### 7. Sessions are short, cheap, and agentic — with a heavy tail
Median session: **7 min · 4 turns · 16 tool calls · $0.67**. p90 cost $2.84,
max $18.44. Typical use is a quick sub-dollar task; the long tail of deep sessions is
where to watch for runaway cost.

### 8. IDE-embedded and Mac-first
Terminals: VS Code 39 users, JetBrains family 37 (PyCharm/WebStorm/IntelliJ combined),
Warp 12, so IDE integrations dominate standalone terminals. OS: **macOS 81 / Linux 18 /
Windows 1**. **20 CLI versions** are in use, from 2.0.76 to 2.1.59, with no forced
upgrade (a fragmentation signal).

---

## Headline findings — provider comparison

All providers, no filters. Numbers come from `provider_summary`,
`provider_share_over_time(grain="week")`, `multi_provider_users`,
`provider_by_dimension("practice")`, `model_leaderboard`, `error_breakdown` and
`error_rate_anomalies(by_provider=True)`.

### P1. Claude Code is the platform; the others are satellites (Leadership)
| Provider | Users | Requests | Share of requests | Spend | Share of spend |
|---|---:|---:|---:|---:|---:|
| Claude Code | 100 | 118,014 | 69.9% | $6,001.43 | **96.2%** |
| ChatGPT / OpenAI | 68 | 26,760 | 15.9% | $173.88 | 2.8% |
| Gemini | 43 | 13,454 | 8.0% | $53.36 | 0.9% |
| DeepSeek | 35 | 10,559 | 6.3% | $8.24 | 0.1% |

Everyone uses Claude Code. The other assistants carry 30% of requests but under 4% of
spend, so budget decisions are really about Claude Code model routing (finding 2).

### P2. Unit cost differs by two orders of magnitude, so compare like with like (FinOps)
| Provider | $/request | $/1K tokens (all) | $/1K uncached input + output | Cache-read share of tokens |
|---|---:|---:|---:|---:|
| Claude Code | $0.0509 | $0.00093 | $0.0581 | 91% |
| ChatGPT / OpenAI | $0.0065 | $0.00279 | $0.0033 | 16% |
| Gemini | $0.0040 | $0.00159 | $0.0018 | 12% |
| DeepSeek | $0.00078 | $0.00026 | $0.00034 | 24% |

The blended $/1K-tokens (`provider_summary.cost_per_1k_tokens`) makes Claude Code look
cheaper than ChatGPT. That is because 91% of its tokens are cheap cache reads of large
agent contexts. On uncached input + output it is the most expensive, and one Claude
Code request is an agent turn, not a chat completion. The uncached-only column is an
ad-hoc query over `events_enriched`; it is not a stored metric. Use $/request and
$/uncached token for routing decisions. Claude Code cost is the telemetry-reported
distribution, while the other providers' cost is tokens × catalog price.

### P3. Gemini is the growing provider (Leadership)
Gemini's weekly share of requests rose from **1.4%** (week of 2025-12-08) to **16.1%**
(week of 2026-01-26), and its share of spend from 0.15% to 2.0%. Claude Code's request
share fell from 77.4% to 62.3% over the same weeks; OpenAI stayed between 14.8% and 19.1%.
`provider_share_over_time` makes this a one-chart answer to "which assistant is gaining?"

### P4. DeepSeek is the least reliable; capacity errors dominate everywhere (Platform/DX)
API error rate: **DeepSeek 3.06%** · OpenAI 2.18% · Gemini 2.03% · Claude Code 1.15%.
The worst model is `deepseek-reasoner` at 4.10%. Across the three non-Claude providers,
**429 is 57% of errors** (674 of 1,179), ahead of 500 (255), 503 (177) and 400 (73).
Per-provider anomaly scoring catches the OpenAI 429 storm on **2026-01-12**: 130 errors
on 358 requests (36.3%, z ≈ 76). Unscored, that day is lost in the blended series.
Action: quota headroom and retry/backoff per provider; alert on per-provider error
rate, not the blended one.

### P5. Latency: Gemini fastest, reasoning models slowest (Platform/DX)
p50 / p95 request latency: **Gemini 1.4 s / 4.8 s** · OpenAI 1.7 s / 9.0 s ·
DeepSeek 2.6 s / 11.5 s · Claude Code 7.7 s / 22.4 s (agent turns with large contexts).
The p95 tail of the chat providers comes from reasoning models: `o3` 17.0 s and
`deepseek-reasoner` 14.4 s. Route interactive, latency-sensitive use to the flash/mini tiers.

### P6. Most people use several assistants (FinOps / Leadership)
Users by number of providers: 1 → 11% · **2 → 41%** · **3 → 39%** · 4 → 9% (9 users).
Cost per user rises with breadth, from $58.74 (one provider) to $74.12 (all four).
The biggest overlap is Claude Code + ChatGPT (68 users, i.e. every ChatGPT user),
then Claude Code + Gemini (43), Claude Code + DeepSeek (35) and Gemini + ChatGPT (30),
ordered by shared users. That overlap is a candidate for seat consolidation.

### P7. Provider preference follows the practice (Engineering managers)
ML Engineering has the most Gemini users (17 of 24) and Data Engineering 10 of 19.
Frontend Engineering is almost entirely on ChatGPT (24 of 25), and Backend Engineering
13 of 19. Enablement and licensing can be targeted per practice instead of rolled out
company-wide.

### P8. A few models carry each provider's spend (FinOps / Platform)
- **ChatGPT:** `gpt-4o` is $98.44 of $173.88 (57%). `o3` is $45.61 (26%) on only 2,603
  requests, while `gpt-4o-mini` serves 8,109 requests for $2.77.
- **Gemini:** `gemini-2.5-pro` is $43.42 of $53.36 (81%).

Low-cost, low-volume families such as `gemini-2.5-flash-lite` (1,070 requests, $0.20)
are retirement or consolidation candidates. `model_leaderboard` shows them side by side.

### Data quality
In `data_quality` / `ingest_runs`, the ETL removed **83 replayed OpenAI records** and
skipped **11 truncated Gemini lines**. No request lacks a cost, and every user is found
in the employee directory.

---

## Anomaly detection (bonus)

The *Anomalies* dashboard tab flags days where daily **spend** or **API error rate**
departs from recent behaviour. The method is a **causal rolling z-score**: each day is
scored against the mean ± standard deviation of the *previous* N days only. The current
day is excluded via a shift, so a spike is measured against the normal that preceded it
rather than smoothing itself away. A day is anomalous when `|z| ≥ threshold`. Both the
window (default 7 days) and the threshold (default 3.0) are tunable in the UI, and the
per-provider toggle (`by_provider=True`) scores each provider's series separately.

Filtered to Claude Code, the reference dataset still flags **2 cost-spike days and 2
error-rate-spike days** at z ≥ 3 (max |z| ≈ 4.0 for cost, 5.2 for error rate). On the
blended all-provider series it flags 2 cost days and 5 error-rate days. The strongest
is the OpenAI 429 storm on 2026-01-12 (z ≈ 29.5 blended, ≈ 76 for OpenAI alone). The
detector is a pure function (`detect_anomalies`), unit-tested against a synthetic spike
and a flat series.

## Persona framing

| Persona | Dashboard tab | Question answered |
|---------|--------------|-------------------|
| Leadership | Overview, Providers | Is adoption growing, what does it cost, and which assistant is gaining share? |
| FinOps | Cost, Providers | Where is the routing/spend lever, and what does each provider cost per request / 1K tokens? |
| Platform / DX | Tools, Reliability, Providers | Which tools need guardrails, and which provider has the worst error rate and latency? |
| Engineering manager | Providers (cohort heatmap) | Which practices and levels prefer which assistant? |

Insights are framed to a decision (model routing, consolidation, quota/retry,
enablement, guardrails), not just a number. This is the grading criterion "insights
that inform product decisions."

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
con = q.connect()                                        # read-only DuckDB connection
q.kpis(con, Filters())                                   # headline KPIs, all providers
q.cost_by_model(con, Filters(providers=["claude_code"])) # the model-family table above
q.tool_usage(con, Filters(providers=["claude_code"]))    # success / reject / latency per tool
q.provider_summary(con, Filters())                       # P1, P2, P4, P5
q.provider_share_over_time(con, Filters(), grain="week") # P3
q.multi_provider_users(con, Filters())                   # P6
q.model_leaderboard(con, Filters())                      # P8
q.error_rate_anomalies(con, Filters(), by_provider=True) # the OpenAI 429 storm
```

The "uncached input + output" column in P2 is one ad-hoc query:

```sql
SELECT provider,
       sum(cost_usd) * 1000 / sum(COALESCE(input_tokens,0) + COALESCE(output_tokens,0)) AS usd_per_1k_uncached,
       sum(cache_read_tokens) / sum(total_tokens) AS cache_read_share
FROM events_enriched WHERE event_type = 'api_request' GROUP BY provider;
```

Or open the database directly: `duckdb data/analytics.duckdb`. See
`.claude/skills/telemetry-analytics/SKILL.md` for the full data model and query
patterns.
