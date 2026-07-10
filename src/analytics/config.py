"""Central configuration, resolved from environment variables with sane defaults.

All paths are absolute so the pipeline behaves identically whether it is invoked
from the repo root, from ``src/``, inside Docker, or from Streamlit.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional; env vars still work without it
    pass

# Repo root = two levels up from this file (src/analytics/config.py -> repo root)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _path(env_var: str, default: str) -> Path:
    value = os.environ.get(env_var, default)
    p = Path(value)
    return p if p.is_absolute() else (REPO_ROOT / p)


# Where generate_fake_data.py writes its output
DATA_DIR: Path = _path("DATA_DIR", "data/output")
TELEMETRY_JSONL: Path = _path("TELEMETRY_JSONL", str(DATA_DIR / "telemetry_logs.jsonl"))
EMPLOYEES_CSV: Path = _path("EMPLOYEES_CSV", str(DATA_DIR / "employees.csv"))

# DuckDB database file produced by the ETL and read by the dashboard
DUCKDB_PATH: Path = _path("DUCKDB_PATH", "data/analytics.duckdb")

# Dataset generation defaults (used by `make data` / the entrypoint)
GEN_NUM_USERS: int = int(os.environ.get("GEN_NUM_USERS", "100"))
GEN_NUM_SESSIONS: int = int(os.environ.get("GEN_NUM_SESSIONS", "5000"))
GEN_DAYS: int = int(os.environ.get("GEN_DAYS", "60"))
GEN_SEED: int = int(os.environ.get("GEN_SEED", "42"))
