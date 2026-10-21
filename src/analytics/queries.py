"""Reusable analytics queries over the DuckDB database.

Every function takes a live DuckDB connection plus optional filters and returns a
pandas DataFrame ready to chart. Filters are applied through a single shared
``_where`` builder so the dashboard can slice every metric consistently and
safely (values are passed as bind parameters, never string-formatted).

Multi-provider: every row carries ``provider`` (claude_code / openai / gemini /
deepseek). With an empty ``Filters.providers`` every metric covers all providers.
Claude-Code-only metrics: ``tool_usage`` decisions/success and ``decision_sources``
(only Claude Code logs tool decisions; OpenAI ``tool_calls`` appear as
``tool_result`` rows with ``success`` NULL), ``prompts`` in :func:`kpis`
(``user_prompt`` events) and ``session_stats.turns``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from . import config
from .providers import PROVIDER_LABELS, PROVIDER_NAMES


@dataclass
class Filters:
    """Dashboard filter state. ``None`` / empty means 'no filter'."""

    start_date: date | None = None
    end_date: date | None = None
    practices: list[str] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)
    # ``models``: raw model ids, exact match (``gpt-4o`` does not match the dated snapshot
    # ``gpt-4o-2024-08-06``); use ``model_families`` to select a normalized family.
    models: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)    # e.g. ["claude_code", "openai"]
    model_families: list[str] = field(default_factory=list)  # normalized family, e.g. "gpt-4o"

    def clause(self) -> tuple[str, list]:
        """Return a ``WHERE`` fragment (without the keyword) and bind params."""
        conds: list[str] = []
        params: list = []
        if self.start_date:
            conds.append("event_date >= ?")
            params.append(self.start_date)
        if self.end_date:
            conds.append("event_date <= ?")
            params.append(self.end_date)
        if self.practices:
            conds.append(f"practice IN ({', '.join('?' * len(self.practices))})")
            params.extend(self.practices)
        if self.levels:
            conds.append(f"level IN ({', '.join('?' * len(self.levels))})")
            params.extend(self.levels)
        if self.models:
            conds.append(f"model IN ({', '.join('?' * len(self.models))})")
            params.extend(self.models)
        if self.model_families:
            conds.append(f"model_family IN ({', '.join('?' * len(self.model_families))})")
            params.extend(self.model_families)
        if self.providers:
            conds.append(f"provider IN ({', '.join('?' * len(self.providers))})")
            params.extend(self.providers)
        return (" AND ".join(conds) if conds else "TRUE"), params


def connect(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    if not config.DUCKDB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {config.DUCKDB_PATH}. Run the ETL first (`make etl`)."
        )
    return duckdb.connect(str(config.DUCKDB_PATH), read_only=read_only)


def _q(con: duckdb.DuckDBPyConnection, sql: str, f: Filters, extra: list | None = None) -> pd.DataFrame:
    where, params = f.clause()
    return con.execute(sql.replace("{where}", where), params + (extra or [])).df()


# --- filter option lookups (unfiltered, for populating widgets) -------------

def filter_options(con: duckdb.DuckDBPyConnection) -> dict:
    def col(name: str) -> list:
        rows = con.execute(
            f"SELECT DISTINCT {name} FROM events_enriched "
            f"WHERE {name} IS NOT NULL ORDER BY {name}"
        ).fetchall()
        return [r[0] for r in rows]

    dr = con.execute(
        "SELECT min(event_date), max(event_date) FROM events_enriched"
    ).fetchone()
    models_by_provider: dict[str, list[str]] = {}
    for provider, family in con.execute(
        "SELECT DISTINCT provider, model_family FROM events_enriched "
        "WHERE model_family IS NOT NULL ORDER BY provider, model_family"
    ).fetchall():
        models_by_provider.setdefault(provider, []).append(family)
    return {
        "date_min": dr[0],
        "date_max": dr[1],
        "practices": col("practice"),
        "levels": [r[0] for r in con.execute(
            "SELECT DISTINCT level FROM events_enriched WHERE level IS NOT NULL "
            "ORDER BY TRY_CAST(regexp_replace(level,'[^0-9]','','g') AS INTEGER)"
        ).fetchall()],
        "models": col("model"),
        "providers": col("provider"),
        "provider_labels": {p: PROVIDER_LABELS.get(p, p) for p in col("provider")},
        "model_families": col("model_family"),
        "models_by_provider": models_by_provider,
        "clients": col("client"),
    }


# --- headline KPIs ----------------------------------------------------------

def kpis(con: duckdb.DuckDBPyConnection, f: Filters) -> dict:
    row = _q(con, """
        SELECT
            count(*) FILTER (WHERE event_type='api_request')                AS api_requests,
            count(DISTINCT session_id)                                      AS sessions,
            count(DISTINCT user_email)                                      AS active_users,
            COALESCE(sum(cost_usd), 0)                                      AS total_cost,
            COALESCE(sum(total_tokens) FILTER (WHERE event_type='api_request'), 0) AS total_tokens,
            count(*) FILTER (WHERE event_type='user_prompt')               AS prompts,
            count(*) FILTER (WHERE event_type='api_error')                 AS errors,
            count(DISTINCT provider)                                        AS providers_in_use
        FROM events_enriched WHERE {where}
    """, f).iloc[0].to_dict()
    return row


# --- time series ------------------------------------------------------------

def daily_activity(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    return _q(con, """
        SELECT event_date,
               COALESCE(sum(cost_usd), 0)                        AS cost_usd,
               count(*) FILTER (WHERE event_type='api_request')  AS api_requests,
               count(DISTINCT session_id)                        AS sessions,
               count(DISTINCT user_email)                        AS active_users
        FROM events_enriched WHERE {where}
        GROUP BY event_date ORDER BY event_date
    """, f)


def activity_heatmap(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    return _q(con, """
        SELECT event_dow, event_hour, count(*) AS events
        FROM events_enriched WHERE {where}
        GROUP BY event_dow, event_hour
    """, f)


# --- cost & tokens ----------------------------------------------------------

def cost_by_model(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    return _q(con, """
        SELECT model,
               count(*)                          AS requests,
               COALESCE(sum(cost_usd), 0)        AS total_cost,
               COALESCE(avg(cost_usd), 0)        AS avg_cost,
               COALESCE(sum(input_tokens), 0)    AS input_tokens,
               COALESCE(sum(output_tokens), 0)   AS output_tokens,
               COALESCE(sum(cache_read_tokens), 0) AS cache_read_tokens
        FROM events_enriched
        WHERE {where} AND event_type='api_request' AND model IS NOT NULL
        GROUP BY model ORDER BY total_cost DESC
    """, f)


def cost_by_practice(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    return _q(con, """
        SELECT COALESCE(practice, 'Unknown') AS practice,
               COALESCE(sum(cost_usd), 0)    AS total_cost,
               count(DISTINCT user_email)    AS users,
               COALESCE(sum(cost_usd), 0) / NULLIF(count(DISTINCT user_email),0) AS cost_per_user
        FROM events_enriched
        WHERE {where} AND event_type='api_request'
        GROUP BY practice ORDER BY total_cost DESC
    """, f)


def cost_by_level(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    return _q(con, """
        SELECT level, level_num,
               COALESCE(sum(cost_usd), 0) AS total_cost,
               count(DISTINCT user_email) AS users,
               COALESCE(sum(cost_usd),0) / NULLIF(count(DISTINCT user_email),0) AS cost_per_user
        FROM events_enriched
        WHERE {where} AND event_type='api_request' AND level IS NOT NULL
        GROUP BY level, level_num ORDER BY level_num
    """, f)


def top_users(con: duckdb.DuckDBPyConnection, f: Filters, limit: int = 15) -> pd.DataFrame:
    return _q(con, """
        SELECT COALESCE(full_name, user_email) AS user,
               practice, level,
               COALESCE(sum(cost_usd), 0)      AS total_cost,
               count(DISTINCT session_id)      AS sessions,
               count(*) FILTER (WHERE event_type='api_request') AS api_requests
        FROM events_enriched WHERE {where}
        GROUP BY user, practice, level
        ORDER BY total_cost DESC LIMIT ?
    """, f, extra=[limit])


# --- tools ------------------------------------------------------------------

def tool_usage(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    return _q(con, """
        SELECT tool_name,
               count(*) FILTER (WHERE event_type='tool_result')                       AS runs,
               count(*) FILTER (WHERE event_type='tool_decision')                      AS decisions,
               count(*) FILTER (WHERE event_type='tool_decision' AND decision='reject') AS rejections,
               avg(CASE WHEN event_type='tool_result' THEN success::INT END)           AS success_rate,
               avg(CASE WHEN event_type='tool_result' THEN duration_ms END)            AS avg_duration_ms
        FROM events_enriched
        WHERE {where} AND tool_name IS NOT NULL
        GROUP BY tool_name ORDER BY runs DESC
    """, f)


def decision_sources(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    return _q(con, """
        SELECT COALESCE(decision_source, 'unknown') AS decision_source,
               count(*) AS decisions
        FROM events_enriched
        WHERE {where} AND event_type='tool_decision'
        GROUP BY decision_source ORDER BY decisions DESC
    """, f)


# --- reliability ------------------------------------------------------------

def error_breakdown(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    return _q(con, """
        SELECT COALESCE(status_code, 'unknown') AS status_code,
               error,
               count(*) AS occurrences
        FROM events_enriched
        WHERE {where} AND event_type='api_error'
        GROUP BY status_code, error ORDER BY occurrences DESC
    """, f)


def error_rate_over_time(
    con: duckdb.DuckDBPyConnection, f: Filters, by_provider: bool = False
) -> pd.DataFrame:
    """Daily errors / requests. ``by_provider=True`` adds a ``provider`` column."""
    key = "event_date, provider" if by_provider else "event_date"
    return _q(con, f"""
        SELECT {key},
               count(*) FILTER (WHERE event_type='api_error')   AS errors,
               count(*) FILTER (WHERE event_type='api_request') AS requests,
               count(*) FILTER (WHERE event_type='api_error')::DOUBLE
                   / NULLIF(count(*) FILTER (WHERE event_type='api_request'),0) AS error_rate
        FROM events_enriched WHERE {{where}}
        GROUP BY {key} ORDER BY {key}
    """, f)


# --- sessions ---------------------------------------------------------------

def session_stats(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    """Per-session rollup: duration, cost, turns, tool calls."""
    return _q(con, """
        WITH s AS (
            SELECT session_id,
                   any_value(practice) AS practice,
                   min(timestamp) AS started,
                   max(timestamp) AS ended,
                   COALESCE(sum(cost_usd), 0) AS cost_usd,
                   count(*) FILTER (WHERE event_type='user_prompt') AS turns,
                   count(*) FILTER (WHERE event_type='tool_result') AS tool_calls,
                   count(*) FILTER (WHERE event_type='api_request') AS api_requests
            FROM events_enriched WHERE {where}
            GROUP BY session_id
        )
        SELECT *, date_diff('second', started, ended) AS duration_s FROM s
    """, f)


# --- adoption ---------------------------------------------------------------

def adoption_by_dimension(con: duckdb.DuckDBPyConnection, f: Filters, column: str) -> pd.DataFrame:
    """Distinct users + activity grouped by an environment column.

    ``column`` is validated against an allow-list to prevent SQL injection since
    it is interpolated into the query text (bind params can't name columns).
    """
    allowed = {"terminal_type", "cli_version", "os_type", "location", "client"}
    if column not in allowed:
        raise ValueError(f"column must be one of {allowed}, got {column!r}")
    return _q(con, f"""
        SELECT COALESCE({column}, 'unknown') AS {column},
               count(DISTINCT user_email) AS users,
               count(DISTINCT session_id) AS sessions,
               count(*)                   AS events
        FROM events_enriched WHERE {{where}}
        GROUP BY {column} ORDER BY users DESC
    """, f)


# --- anomaly detection -----------------------------------------------------

def detect_anomalies(
    series: pd.DataFrame,
    value_col: str,
    window: int = 7,
    z_thresh: float = 3.0,
) -> pd.DataFrame:
    """Flag anomalies in a daily series with a *causal* rolling z-score.

    For each day the baseline mean and spread are computed over the ``window``
    *previous* days only (the current day is excluded via a shift), so a spike is
    judged against normal behaviour that preceded it rather than smoothing itself
    away. ``z_score = (value - trailing_mean) / trailing_std``; a day is anomalous
    when ``|z_score| >= z_thresh`` and enough history exists.

    Returns the input frame plus ``baseline``, ``z_score`` and ``is_anomaly``
    columns. Pure/self-contained so it is unit-tested without a database.
    """
    df = series.copy().reset_index(drop=True)
    values = df[value_col].astype(float)
    prior = values.shift(1)  # exclude the current day -> causal baseline
    min_periods = max(3, window // 2)
    baseline = prior.rolling(window, min_periods=min_periods).mean()
    spread = prior.rolling(window, min_periods=min_periods).std(ddof=0)
    z = (values - baseline) / spread.replace(0, np.nan)
    df["baseline"] = baseline
    df["z_score"] = z
    df["is_anomaly"] = (z.abs() >= z_thresh).fillna(False)
    return df


def _per_provider(daily: pd.DataFrame, value_col: str, window: int, z_thresh: float) -> pd.DataFrame:
    """Run the detector independently on each provider's daily series."""
    parts = [
        detect_anomalies(group.drop(columns="provider"), value_col, window, z_thresh)
        .assign(provider=provider)
        for provider, group in daily.groupby("provider", sort=True)
    ]
    out = pd.concat(parts, ignore_index=True)
    cols = ["provider"] + [c for c in out.columns if c != "provider"]
    return out[cols]


def cost_anomalies(
    con: duckdb.DuckDBPyConnection, f: Filters, window: int = 7, z_thresh: float = 3.0,
    by_provider: bool = False,
) -> pd.DataFrame:
    """Daily spend with anomalous days flagged (see :func:`detect_anomalies`).

    ``by_provider=True`` scores each provider's series separately and adds ``provider``.
    """
    key = "provider, event_date" if by_provider else "event_date"
    daily = _q(con, f"""
        SELECT {key}, COALESCE(sum(cost_usd), 0) AS cost_usd
        FROM events_enriched WHERE {{where}}
        GROUP BY {key} ORDER BY {key}
    """, f)
    if daily.empty:
        return daily.assign(baseline=[], z_score=[], is_anomaly=[])
    if by_provider:
        return _per_provider(daily, "cost_usd", window, z_thresh)
    return detect_anomalies(daily, "cost_usd", window, z_thresh)


def error_rate_anomalies(
    con: duckdb.DuckDBPyConnection, f: Filters, window: int = 7, z_thresh: float = 3.0,
    by_provider: bool = False,
) -> pd.DataFrame:
    """Daily API error rate with anomalous days flagged (``by_provider`` as in
    :func:`cost_anomalies`)."""
    key = "provider, event_date" if by_provider else "event_date"
    daily = _q(con, f"""
        SELECT {key},
               count(*) FILTER (WHERE event_type='api_error')::DOUBLE
                 / NULLIF(count(*) FILTER (WHERE event_type='api_request'), 0) AS error_rate,
               count(*) FILTER (WHERE event_type='api_error')   AS errors,
               count(*) FILTER (WHERE event_type='api_request') AS requests
        FROM events_enriched WHERE {{where}}
        GROUP BY {key} ORDER BY {key}
    """, f)
    if daily.empty:
        return daily.assign(baseline=[], z_score=[], is_anomaly=[])
    daily["error_rate"] = daily["error_rate"].fillna(0.0)
    if by_provider:
        return _per_provider(daily, "error_rate", window, z_thresh)
    return detect_anomalies(daily, "error_rate", window, z_thresh)


# --- multi-provider ---------------------------------------------------------
# Conventions: "requests" = api_request rows; error_rate = api_error / api_request
# (same definition as error_rate_over_time); latency = api_request duration_ms;
# cost_per_1k_tokens only divides by tokens of requests that *have* a cost, so a
# request with a missing/malformed cost doesn't dilute it.

def valid_providers() -> list[str]:
    """Every provider id the platform knows (for validating ``Filters.providers``)."""
    return list(PROVIDER_NAMES)


def provider_catalog(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Every known provider (registry order) with its label and loaded event count —
    unfiltered, like :func:`filter_options`. ``has_data`` is False for a provider whose
    optional source file was missing at ETL time."""
    counts = dict(con.execute(
        "SELECT provider, count(*) FROM events GROUP BY provider").fetchall())
    return pd.DataFrame(
        [{"provider": p, "label": PROVIDER_LABELS.get(p, p),
          "events": int(counts.get(p, 0)), "has_data": p in counts}
         for p in valid_providers()],
        columns=["provider", "label", "events", "has_data"],
    )


def provider_summary(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    """One row per provider: volume, users, cost, tokens, reliability, latency, unit cost."""
    return _q(con, """
        WITH p AS (
            SELECT provider, any_value(provider_label) AS provider_label,
                   count(*) FILTER (WHERE event_type='api_request')         AS requests,
                   count(*) FILTER (WHERE event_type='api_error')           AS errors,
                   count(DISTINCT user_email)                               AS users,
                   count(DISTINCT session_id)                               AS sessions,
                   COALESCE(sum(cost_usd), 0)                               AS total_cost,
                   COALESCE(sum(input_tokens)      FILTER (WHERE event_type='api_request'), 0) AS input_tokens,
                   COALESCE(sum(output_tokens)     FILTER (WHERE event_type='api_request'), 0) AS output_tokens,
                   COALESCE(sum(cache_read_tokens) FILTER (WHERE event_type='api_request'), 0) AS cache_read_tokens,
                   COALESCE(sum(reasoning_tokens)  FILTER (WHERE event_type='api_request'), 0) AS reasoning_tokens,
                   COALESCE(sum(total_tokens)      FILTER (WHERE event_type='api_request'), 0) AS total_tokens,
                   sum(total_tokens) FILTER (WHERE event_type='api_request' AND cost_usd IS NOT NULL)
                                                                            AS priced_tokens,
                   count(*) FILTER (WHERE event_type='api_request' AND cost_usd IS NULL) AS unpriced_requests,
                   quantile_cont(duration_ms, 0.5)  FILTER (WHERE event_type='api_request') AS p50_latency_ms,
                   quantile_cont(duration_ms, 0.95) FILTER (WHERE event_type='api_request') AS p95_latency_ms
            FROM events_enriched WHERE {where}
            GROUP BY provider
        )
        SELECT provider, provider_label, requests, errors,
               errors::DOUBLE / NULLIF(requests, 0)                   AS error_rate,
               users, sessions, total_cost,
               input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, total_tokens,
               unpriced_requests, p50_latency_ms, p95_latency_ms,
               total_cost / NULLIF(users, 0)                          AS cost_per_user,
               total_cost / NULLIF(requests, 0)                       AS cost_per_request,
               total_cost * 1000.0 / NULLIF(priced_tokens, 0)         AS cost_per_1k_tokens
        FROM p ORDER BY total_cost DESC, provider
    """, f)


_GRAINS = {"day": "event_date", "week": "CAST(event_week AS DATE)",
           "month": "CAST(date_trunc('month', event_date) AS DATE)"}


def provider_share_over_time(
    con: duckdb.DuckDBPyConnection, f: Filters, grain: str = "week"
) -> pd.DataFrame:
    """Per period x provider: absolute ``total_cost``/``requests``/``users`` plus each
    provider's share of the period (``*_share``; users share is of provider-user pairs,
    so a person on two providers counts for both)."""
    if grain not in _GRAINS:
        raise ValueError(f"grain must be one of {sorted(_GRAINS)}, got {grain!r}")
    period = _GRAINS[grain]
    return _q(con, f"""
        WITH t AS (
            SELECT {period} AS period, provider, any_value(provider_label) AS provider_label,
                   COALESCE(sum(cost_usd), 0)                       AS total_cost,
                   count(*) FILTER (WHERE event_type='api_request') AS requests,
                   count(DISTINCT user_email)                       AS users
            FROM events_enriched WHERE {{where}}
            GROUP BY 1, provider
        )
        SELECT *,
               total_cost / NULLIF(sum(total_cost) OVER (PARTITION BY period), 0)       AS cost_share,
               requests::DOUBLE / NULLIF(sum(requests) OVER (PARTITION BY period), 0)  AS requests_share,
               users::DOUBLE / NULLIF(sum(users) OVER (PARTITION BY period), 0)        AS users_share
        FROM t ORDER BY period, provider
    """, f)


def provider_by_dimension(con: duckdb.DuckDBPyConnection, f: Filters, column: str) -> pd.DataFrame:
    """``column`` x provider matrix (users, requests, cost). ``column`` is allow-listed
    because it is interpolated (bind params can't name columns)."""
    allowed = {"practice", "level", "location", "client", "model_family"}
    if column not in allowed:
        raise ValueError(f"column must be one of {allowed}, got {column!r}")
    order = "min(level_num) NULLS LAST, 1, provider" if column == "level" else "1, provider"
    return _q(con, f"""
        SELECT COALESCE({column}, 'Unknown') AS {column},
               provider, any_value(provider_label) AS provider_label,
               count(DISTINCT user_email)                        AS users,
               count(*) FILTER (WHERE event_type='api_request')  AS requests,
               COALESCE(sum(cost_usd), 0)                        AS total_cost,
               COALESCE(sum(cost_usd), 0) / NULLIF(count(DISTINCT user_email), 0) AS cost_per_user
        FROM events_enriched WHERE {{where}}
        GROUP BY 1, provider ORDER BY {order}
    """, f)


def multi_provider_users(con: duckdb.DuckDBPyConnection, f: Filters) -> dict[str, pd.DataFrame]:
    """How many providers each (attributed) user uses.

    Returns ``{"distribution": [n_providers, users, share, total_cost, cost_per_user],
    "pairs": [provider_a, provider_b, users, jaccard]}`` (pairs: users active on both).
    """
    distribution = _q(con, """
        WITH u AS (
            SELECT user_email, count(DISTINCT provider) AS n_providers,
                   COALESCE(sum(cost_usd), 0) AS cost
            FROM events_enriched WHERE {where} AND user_email IS NOT NULL
            GROUP BY user_email
        )
        SELECT n_providers, count(*) AS users,
               count(*)::DOUBLE / sum(count(*)) OVER () AS share,
               sum(cost) AS total_cost, sum(cost) / count(*) AS cost_per_user
        FROM u GROUP BY n_providers ORDER BY n_providers
    """, f)
    pairs = _q(con, """
        WITH up AS (
            SELECT DISTINCT user_email, provider
            FROM events_enriched WHERE {where} AND user_email IS NOT NULL
        ),
        n AS (SELECT provider, count(*) AS users FROM up GROUP BY provider)
        SELECT a.provider AS provider_a, b.provider AS provider_b,
               count(*) AS users,
               count(*)::DOUBLE / (any_value(na.users) + any_value(nb.users) - count(*)) AS jaccard
        FROM up a JOIN up b ON a.user_email = b.user_email AND a.provider < b.provider
        JOIN n na ON na.provider = a.provider
        JOIN n nb ON nb.provider = b.provider
        GROUP BY a.provider, b.provider ORDER BY users DESC, provider_a, provider_b
    """, f)
    return {"distribution": distribution, "pairs": pairs}


def model_leaderboard(
    con: duckdb.DuckDBPyConnection, f: Filters, limit: int | None = None
) -> pd.DataFrame:
    """Per provider x ``model_family``: usage, cost, unit cost, reliability, latency and
    recency — which models carry the spend, which could be retired."""
    sql = """
        SELECT provider, any_value(provider_label) AS provider_label, model_family,
               count(*) FILTER (WHERE event_type='api_request')                  AS requests,
               count(*) FILTER (WHERE event_type='api_error')                    AS errors,
               count(*) FILTER (WHERE event_type='api_error')::DOUBLE
                   / NULLIF(count(*) FILTER (WHERE event_type='api_request'), 0) AS error_rate,
               count(DISTINCT user_email)                                        AS users,
               COALESCE(sum(cost_usd), 0)                                        AS total_cost,
               avg(cost_usd) FILTER (WHERE event_type='api_request')             AS avg_cost,
               COALESCE(sum(total_tokens) FILTER (WHERE event_type='api_request'), 0) AS total_tokens,
               sum(cost_usd) * 1000.0 / NULLIF(sum(total_tokens)
                   FILTER (WHERE event_type='api_request' AND cost_usd IS NOT NULL), 0) AS cost_per_1k_tokens,
               quantile_cont(duration_ms, 0.5)  FILTER (WHERE event_type='api_request') AS p50_latency_ms,
               quantile_cont(duration_ms, 0.95) FILTER (WHERE event_type='api_request') AS p95_latency_ms,
               min(event_date) AS first_used, max(event_date) AS last_used
        FROM events_enriched
        WHERE {where} AND model_family IS NOT NULL
        GROUP BY provider, model_family
        ORDER BY total_cost DESC, requests DESC
    """
    if limit is not None:
        return _q(con, sql + " LIMIT ?", f, extra=[int(limit)])
    return _q(con, sql, f)


def _has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()[0])


def data_quality(con: duckdb.DuckDBPyConnection, f: Filters | None = None) -> pd.DataFrame:
    """Latest ETL audit per provider (from ``ingest_runs``) joined with live counts of
    requests without a cost and user emails missing from the employee directory.
    ``unknown_models`` lists models whose requests had no usable ``cost_usd``.
    Empty frame on a pre-multi-provider DB."""
    cols = ["provider", "provider_label", "source_file", "status", "rows_read", "rows_loaded",
            "rows_skipped", "duplicates_removed", "unknown_models", "unpriced_requests",
            "users_without_employee", "run_at"]
    if not _has_table(con, "ingest_runs"):
        return pd.DataFrame(columns=cols)
    return _q(con, """
        WITH live AS (
            SELECT provider,
                   count(*) FILTER (WHERE event_type='api_request' AND cost_usd IS NULL) AS unpriced_requests,
                   count(DISTINCT user_email) FILTER (WHERE full_name IS NULL)           AS users_without_employee
            FROM events_enriched WHERE {where}
            GROUP BY provider
        )
        SELECT r.provider, COALESCE(pl.provider_label, r.provider) AS provider_label,
               r.source_file, r.status, r.rows_read, r.rows_loaded, r.rows_skipped,
               r.duplicates_removed, r.unknown_models,
               COALESCE(l.unpriced_requests, 0)      AS unpriced_requests,
               COALESCE(l.users_without_employee, 0) AS users_without_employee,
               r.run_at
        FROM ingest_runs r
        LEFT JOIN live l ON l.provider = r.provider
        LEFT JOIN provider_labels pl ON pl.provider = r.provider
        ORDER BY r.rows_loaded DESC
    """, f or Filters())
