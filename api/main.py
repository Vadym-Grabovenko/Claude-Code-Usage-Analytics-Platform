"""FastAPI layer over the analytics query functions.

Programmatic, **read-only** access to exactly the metrics the dashboard shows — the
query layer in ``src/analytics/queries.py`` is reused verbatim, so the API, the
dashboard, and the tests all compute numbers the same way. Every list endpoint accepts
the same optional filters as the dashboard (``start_date``, ``end_date``, repeated
``practices`` / ``levels`` / ``models``) and returns JSON.

Run:  ``uvicorn api.main:app --host 0.0.0.0 --port 8000``   (interactive docs at /docs)
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

# Make the src package importable whether run from repo root, Docker, or elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException, Query  # noqa: E402

from analytics import queries  # noqa: E402
from analytics.queries import Filters  # noqa: E402

app = FastAPI(
    title="Claude Code Usage Analytics API",
    description="Programmatic, read-only access to the telemetry analytics metrics. "
                "All list endpoints accept the same filters as the dashboard.",
    version="1.0.0",
)


# --- dependencies -----------------------------------------------------------

def get_con():
    """Yield a fresh read-only DuckDB connection per request (cheap; thread-safe).

    Returns 503 while the database is missing or still being (re)built by the ETL,
    so a caller during startup gets a clear, retryable signal rather than a 500.
    """
    try:
        con = queries.connect(read_only=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except duckdb.Error as exc:  # e.g. the ETL still holds the write lock
        raise HTTPException(status_code=503, detail=f"database not ready: {exc}")
    try:
        yield con
    finally:
        con.close()


def get_filters(
    start_date: Optional[date] = Query(None, description="inclusive lower bound (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="inclusive upper bound (YYYY-MM-DD)"),
    practices: Optional[list[str]] = Query(None, description="repeatable"),
    levels: Optional[list[str]] = Query(None, description="repeatable, e.g. L5"),
    models: Optional[list[str]] = Query(None, description="repeatable"),
) -> Filters:
    return Filters(
        start_date=start_date, end_date=end_date,
        practices=practices or [], levels=levels or [], models=models or [],
    )


# --- JSON helpers (guarantee native types: numpy -> int/float, dates -> ISO) ---

def _records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", date_format="iso"))


def _obj(mapping) -> dict:
    return json.loads(pd.Series(mapping).to_json())


# --- meta -------------------------------------------------------------------

@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/filters", tags=["meta"])
def filter_options(con=Depends(get_con)):
    """Distinct filter values + date range, for populating a client UI."""
    return _obj(queries.filter_options(con))


# --- headline ---------------------------------------------------------------

@app.get("/kpis", tags=["overview"])
def kpis(con=Depends(get_con), f: Filters = Depends(get_filters)):
    return _obj(queries.kpis(con, f))


@app.get("/activity/daily", tags=["overview"])
def activity_daily(con=Depends(get_con), f: Filters = Depends(get_filters)):
    return _records(queries.daily_activity(con, f))


@app.get("/activity/heatmap", tags=["overview"])
def activity_heatmap(con=Depends(get_con), f: Filters = Depends(get_filters)):
    return _records(queries.activity_heatmap(con, f))


# --- cost -------------------------------------------------------------------

@app.get("/cost/by-model", tags=["cost"])
def cost_by_model(con=Depends(get_con), f: Filters = Depends(get_filters)):
    return _records(queries.cost_by_model(con, f))


@app.get("/cost/by-practice", tags=["cost"])
def cost_by_practice(con=Depends(get_con), f: Filters = Depends(get_filters)):
    return _records(queries.cost_by_practice(con, f))


@app.get("/cost/by-level", tags=["cost"])
def cost_by_level(con=Depends(get_con), f: Filters = Depends(get_filters)):
    return _records(queries.cost_by_level(con, f))


@app.get("/users/top", tags=["cost"])
def top_users(limit: int = Query(15, ge=1, le=100),
              con=Depends(get_con), f: Filters = Depends(get_filters)):
    return _records(queries.top_users(con, f, limit=limit))


# --- tools ------------------------------------------------------------------

@app.get("/tools", tags=["tools"])
def tools(con=Depends(get_con), f: Filters = Depends(get_filters)):
    return _records(queries.tool_usage(con, f))


@app.get("/tools/decision-sources", tags=["tools"])
def decision_sources(con=Depends(get_con), f: Filters = Depends(get_filters)):
    return _records(queries.decision_sources(con, f))


# --- reliability ------------------------------------------------------------

@app.get("/errors", tags=["reliability"])
def errors(con=Depends(get_con), f: Filters = Depends(get_filters)):
    return _records(queries.error_breakdown(con, f))


@app.get("/errors/rate", tags=["reliability"])
def error_rate(con=Depends(get_con), f: Filters = Depends(get_filters)):
    return _records(queries.error_rate_over_time(con, f))


# --- sessions ---------------------------------------------------------------

@app.get("/sessions/summary", tags=["sessions"])
def sessions_summary(con=Depends(get_con), f: Filters = Depends(get_filters)):
    """Aggregate session stats (the per-session table would be too large to return)."""
    s = queries.session_stats(con, f)
    if s.empty:
        return {"sessions": 0}
    return {
        "sessions": int(len(s)),
        "median_duration_s": float(s["duration_s"].median()),
        "median_turns": float(s["turns"].median()),
        "median_tool_calls": float(s["tool_calls"].median()),
        "median_cost_usd": float(s["cost_usd"].median()),
        "p90_cost_usd": float(s["cost_usd"].quantile(0.9)),
        "max_cost_usd": float(s["cost_usd"].max()),
        "total_cost_usd": float(s["cost_usd"].sum()),
    }


# --- adoption ---------------------------------------------------------------

@app.get("/adoption/{dimension}", tags=["adoption"])
def adoption(dimension: str, con=Depends(get_con), f: Filters = Depends(get_filters)):
    """`dimension` ∈ {terminal_type, cli_version, os_type, location}."""
    try:
        return _records(queries.adoption_by_dimension(con, f, dimension))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --- anomalies (bonus) ------------------------------------------------------

@app.get("/anomalies/cost", tags=["anomalies"])
def cost_anomalies(window: int = Query(7, ge=3, le=30),
                   z_thresh: float = Query(3.0, ge=1.0, le=6.0),
                   con=Depends(get_con), f: Filters = Depends(get_filters)):
    """Daily spend with anomalous days flagged (causal rolling z-score)."""
    return _records(queries.cost_anomalies(con, f, window=window, z_thresh=z_thresh))


@app.get("/anomalies/errors", tags=["anomalies"])
def error_anomalies(window: int = Query(7, ge=3, le=30),
                    z_thresh: float = Query(3.0, ge=1.0, le=6.0),
                    con=Depends(get_con), f: Filters = Depends(get_filters)):
    """Daily API error rate with anomalous days flagged."""
    return _records(queries.error_rate_anomalies(con, f, window=window, z_thresh=z_thresh))
