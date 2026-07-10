"""Reusable analytics queries over the DuckDB database.

Every function takes a live DuckDB connection plus optional filters and returns a
pandas DataFrame ready to chart. Filters are applied through a single shared
``_where`` builder so the dashboard can slice every metric consistently and
safely (values are passed as bind parameters, never string-formatted).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from . import config


@dataclass
class Filters:
    """Dashboard filter state. ``None`` / empty means 'no filter'."""

    start_date: date | None = None
    end_date: date | None = None
    practices: list[str] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

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
    return {
        "date_min": dr[0],
        "date_max": dr[1],
        "practices": col("practice"),
        "levels": [r[0] for r in con.execute(
            "SELECT DISTINCT level FROM events_enriched WHERE level IS NOT NULL "
            "ORDER BY TRY_CAST(regexp_replace(level,'[^0-9]','','g') AS INTEGER)"
        ).fetchall()],
        "models": col("model"),
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
            count(*) FILTER (WHERE event_type='api_error')                 AS errors
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


def error_rate_over_time(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    return _q(con, """
        SELECT event_date,
               count(*) FILTER (WHERE event_type='api_error')   AS errors,
               count(*) FILTER (WHERE event_type='api_request') AS requests,
               count(*) FILTER (WHERE event_type='api_error')::DOUBLE
                   / NULLIF(count(*) FILTER (WHERE event_type='api_request'),0) AS error_rate
        FROM events_enriched WHERE {where}
        GROUP BY event_date ORDER BY event_date
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
    allowed = {"terminal_type", "cli_version", "os_type", "location"}
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


def cost_anomalies(
    con: duckdb.DuckDBPyConnection, f: Filters, window: int = 7, z_thresh: float = 3.0
) -> pd.DataFrame:
    """Daily spend with anomalous days flagged (see :func:`detect_anomalies`)."""
    daily = _q(con, """
        SELECT event_date, COALESCE(sum(cost_usd), 0) AS cost_usd
        FROM events_enriched WHERE {where}
        GROUP BY event_date ORDER BY event_date
    """, f)
    if daily.empty:
        return daily.assign(baseline=[], z_score=[], is_anomaly=[])
    return detect_anomalies(daily, "cost_usd", window, z_thresh)


def error_rate_anomalies(
    con: duckdb.DuckDBPyConnection, f: Filters, window: int = 7, z_thresh: float = 3.0
) -> pd.DataFrame:
    """Daily API error rate with anomalous days flagged."""
    daily = _q(con, """
        SELECT event_date,
               count(*) FILTER (WHERE event_type='api_error')::DOUBLE
                 / NULLIF(count(*) FILTER (WHERE event_type='api_request'), 0) AS error_rate,
               count(*) FILTER (WHERE event_type='api_error')   AS errors,
               count(*) FILTER (WHERE event_type='api_request') AS requests
        FROM events_enriched WHERE {where}
        GROUP BY event_date ORDER BY event_date
    """, f)
    if daily.empty:
        return daily.assign(baseline=[], z_score=[], is_anomaly=[])
    daily["error_rate"] = daily["error_rate"].fillna(0.0)
    return detect_anomalies(daily, "error_rate", window, z_thresh)
