"""Build the DuckDB analytics database from the raw telemetry + employee files.

Pipeline: stream + normalize events (ingest.py) -> load into a wide ``events``
fact table -> load the ``employees`` dimension -> create an ``events_enriched``
view that joins them and derives convenience columns (date, hour, cost buckets).

Run directly:  python -m analytics.etl
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

from . import config
from .ingest import IngestStats, iter_events

_CHUNK = 50_000  # rows per insert; keeps peak memory bounded on large datasets


def _load_events(con: duckdb.DuckDBPyConnection, jsonl_path: Path) -> IngestStats:
    """Stream normalized events into the ``events`` table in chunks."""
    stats = IngestStats()
    con.execute("DROP TABLE IF EXISTS events")

    buffer: list[dict] = []
    created = False

    def flush() -> None:
        nonlocal created
        if not buffer:
            return
        frame = pd.DataFrame(buffer)  # noqa: F841 — referenced by DuckDB below
        if not created:
            con.execute("CREATE TABLE events AS SELECT * FROM frame")
            created = True
        else:
            con.execute("INSERT INTO events SELECT * FROM frame")
        buffer.clear()

    for row in iter_events(jsonl_path, stats):
        buffer.append(row)
        if len(buffer) >= _CHUNK:
            flush()
    flush()

    if not created:
        raise RuntimeError(
            f"No valid events parsed from {jsonl_path}. Is the file empty or malformed?"
        )
    return stats


def _load_employees(con: duckdb.DuckDBPyConnection, csv_path: Path) -> int:
    """Load the employee directory dimension. Returns row count (0 if absent)."""
    con.execute("DROP TABLE IF EXISTS employees")
    if not csv_path.exists():
        print(f"  ! employees CSV not found at {csv_path}; proceeding without it.")
        con.execute(
            "CREATE TABLE employees (email VARCHAR, full_name VARCHAR, "
            "practice VARCHAR, level VARCHAR, location VARCHAR)"
        )
        return 0
    con.execute(
        "CREATE TABLE employees AS SELECT * FROM read_csv_auto(?, header=true)",
        [str(csv_path)],
    )
    return con.execute("SELECT count(*) FROM employees").fetchone()[0]


def _build_views(con: duckdb.DuckDBPyConnection) -> None:
    """Create the enriched view every dashboard query builds on."""
    con.execute("DROP VIEW IF EXISTS events_enriched")
    con.execute(
        """
        CREATE VIEW events_enriched AS
        SELECT
            e.*,
            emp.full_name,
            -- CAST guards the all-NULL case: if no row carries a resource practice,
            -- DuckDB types that column as INTEGER and COALESCE would refuse to mix it
            -- with the VARCHAR employee practice.
            COALESCE(emp.practice, CAST(e.resource_practice AS VARCHAR)) AS practice,
            emp.level,
            emp.location,
            -- numeric seniority for ordering (L4 -> 4); NULL if unknown
            TRY_CAST(regexp_replace(emp.level, '[^0-9]', '', 'g') AS INTEGER) AS level_num,
            CAST(e.timestamp AS DATE)              AS event_date,
            date_trunc('week', e.timestamp)        AS event_week,
            EXTRACT(hour FROM e.timestamp)         AS event_hour,
            EXTRACT(dow  FROM e.timestamp)         AS event_dow,
            (e.input_tokens + e.output_tokens
                + e.cache_read_tokens + e.cache_creation_tokens) AS total_tokens
        FROM events e
        LEFT JOIN employees emp ON e.user_email = emp.email
        """
    )


def build_database(
    jsonl_path: Path | None = None,
    csv_path: Path | None = None,
    duckdb_path: Path | None = None,
) -> IngestStats:
    """Full ETL. Returns ingest stats for reporting/tests."""
    jsonl_path = jsonl_path or config.TELEMETRY_JSONL
    csv_path = csv_path or config.EMPLOYEES_CSV
    duckdb_path = duckdb_path or config.DUCKDB_PATH

    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    if duckdb_path.exists():
        duckdb_path.unlink()  # rebuild from scratch for deterministic results

    print(f"→ Building {duckdb_path}")
    con = duckdb.connect(str(duckdb_path))
    try:
        print("  • parsing telemetry events …")
        stats = _load_events(con, jsonl_path)
        print("  • loading employee directory …")
        emp_count = _load_employees(con, csv_path)
        print("  • building enriched view …")
        _build_views(con)
        con.execute("CHECKPOINT")
    finally:
        con.close()

    print("\n=== ETL summary ===")
    print(f"  batches parsed        : {stats.batches:,}")
    print(f"  events loaded         : {stats.events:,}")
    print(f"  employees loaded      : {emp_count:,}")
    print(f"  skipped (bad lines)   : {stats.skipped_lines:,}")
    print(f"  skipped (bad events)  : {stats.skipped_events:,}")
    for etype, count in sorted(stats.by_event_type.items(), key=lambda kv: -kv[1]):
        print(f"    - {etype:<16}: {count:,}")
    if stats.errors:
        print(f"  first {len(stats.errors)} parse warnings:")
        for msg in stats.errors[:5]:
            print(f"    ! {msg}")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the DuckDB analytics database.")
    parser.add_argument("--jsonl", type=Path, default=None, help="telemetry_logs.jsonl path")
    parser.add_argument("--csv", type=Path, default=None, help="employees.csv path")
    parser.add_argument("--db", type=Path, default=None, help="output DuckDB path")
    args = parser.parse_args(argv)
    try:
        build_database(args.jsonl, args.csv, args.db)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
