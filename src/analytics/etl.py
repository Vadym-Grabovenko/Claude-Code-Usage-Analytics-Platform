"""Build the DuckDB analytics database from every enabled provider's usage log.

Pipeline:
  1. stream canonical rows from each provider adapter (``analytics.providers``) into
     one wide ``events`` fact table — a missing *optional* provider file is a warning,
     the Claude Code file stays mandatory;
  2. load the ``employees`` dimension and the ``provider_labels`` lookup;
  3. de-duplicate on ``(provider, source_event_id)``;
  4. record requests without a usable ``cost_usd`` (every source reports cost; a
     NULL means a missing/malformed value) as data-quality findings;
  5. write the ``ingest_runs`` audit and build the ``events_enriched`` view.

Run directly:  python -m analytics.etl [--providers claude_code,openai]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from . import config
from .ingest import IngestStats
from .providers import CANONICAL_COLUMNS, CANONICAL_SCHEMA, PROVIDER_LABELS, BaseAdapter, get_adapters

_CHUNK = 50_000  # rows per insert; keeps peak memory bounded on large datasets


# --- events -----------------------------------------------------------------

def _create_events_table(con: duckdb.DuckDBPyConnection) -> None:
    """Declare the canonical schema up front (names/types are code constants), so a
    chunk whose optional columns happen to be all-NULL can't fix a wrong type."""
    con.execute("DROP TABLE IF EXISTS events")
    cols = ", ".join(f'"{name}" {sql_type}' for name, sql_type in CANONICAL_SCHEMA)
    con.execute(f"CREATE TABLE events ({cols})")


def _insert_rows(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> None:
    frame = pd.DataFrame.from_records(rows, columns=CANONICAL_COLUMNS)  # noqa: F841 — used by DuckDB
    con.execute("INSERT INTO events BY NAME SELECT * FROM frame")


def _load_provider(con: duckdb.DuckDBPyConnection, adapter: BaseAdapter, stats: IngestStats) -> None:
    """Stream one adapter's canonical rows into ``events`` in bounded chunks."""
    buffer: list[dict] = []
    for row in adapter.iter_events(stats):
        buffer.append(row)
        if len(buffer) >= _CHUNK:
            _insert_rows(con, buffer)
            buffer.clear()
    if buffer:
        _insert_rows(con, buffer)
    stats.note_provider(adapter.name, stats.events)


def _load_events(
    con: duckdb.DuckDBPyConnection, adapters: list[BaseAdapter]
) -> tuple[IngestStats, dict[str, IngestStats], dict[str, str]]:
    """Load every adapter. Returns (total stats, per-provider stats, per-provider status)."""
    _create_events_table(con)
    total = IngestStats()
    per_provider: dict[str, IngestStats] = {}
    status: dict[str, str] = {}
    for adapter in adapters:
        pstats = IngestStats()
        per_provider[adapter.name] = pstats
        if not adapter.required and not adapter.available():
            status[adapter.name] = "missing"
            msg = (f"{adapter.label} source not found at {adapter.paths['events']}; skipped "
                   f"(run `make data` to generate it)")
            print(f"  ! {msg}")
            pstats.warn(msg)
            total.merge(pstats)
            continue
        print(f"  • parsing {adapter.label} events …")
        _load_provider(con, adapter, pstats)
        status[adapter.name] = "ok"
        total.merge(pstats)

    if total.events == 0:
        sources = ", ".join(str(a.paths["events"]) for a in adapters)
        raise RuntimeError(f"No valid events parsed from {sources}. Are the files empty or malformed?")
    return total, per_provider, status


def _dedupe(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Drop repeated ``(provider, source_event_id)`` rows (gateway replays, re-shipped
    batches), keeping the first loaded. Rows without an id are never touched."""
    dupes = dict(con.execute("""
        SELECT provider, count(*) - count(DISTINCT source_event_id)
        FROM events WHERE source_event_id IS NOT NULL
        GROUP BY provider
    """).fetchall())
    if any(dupes.values()):
        con.execute("""
            DELETE FROM events WHERE rowid IN (
                SELECT rid FROM (
                    SELECT rowid AS rid,
                           row_number() OVER (PARTITION BY provider, source_event_id
                                              ORDER BY rowid) AS rn
                    FROM events WHERE source_event_id IS NOT NULL
                ) WHERE rn > 1
            )
        """)
    return {k: int(v) for k, v in dupes.items() if v}


def _unpriced_models(con: duckdb.DuckDBPyConnection) -> dict[str, set[str]]:
    """Models with ``api_request`` rows whose cost is missing/malformed, per provider."""
    unknown: dict[str, set[str]] = {}
    for provider, family in con.execute("""
        SELECT DISTINCT provider, COALESCE(model_family, model, '<none>')
        FROM events WHERE event_type = 'api_request' AND cost_usd IS NULL
        ORDER BY 1, 2
    """).fetchall():
        unknown.setdefault(provider, set()).add(family)
    return unknown


# --- dimensions ---------------------------------------------------------------

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


def _load_provider_labels(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("DROP TABLE IF EXISTS provider_labels")
    con.execute("CREATE TABLE provider_labels (provider VARCHAR, provider_label VARCHAR)")
    con.executemany("INSERT INTO provider_labels VALUES (?, ?)", list(PROVIDER_LABELS.items()))


# --- audit + views ------------------------------------------------------------

def _write_ingest_runs(
    con: duckdb.DuckDBPyConnection,
    adapters: list[BaseAdapter],
    per_provider: dict[str, IngestStats],
    status: dict[str, str],
    run_at: datetime,
) -> None:
    con.execute("DROP TABLE IF EXISTS ingest_runs")
    con.execute("""
        CREATE TABLE ingest_runs (
            run_at TIMESTAMP WITH TIME ZONE, provider VARCHAR, source_file VARCHAR,
            status VARCHAR, rows_read BIGINT, rows_loaded BIGINT, rows_skipped BIGINT,
            duplicates_removed BIGINT, unknown_models VARCHAR
        )
    """)
    loaded = dict(con.execute("SELECT provider, count(*) FROM events GROUP BY 1").fetchall())
    for adapter in adapters:
        s = per_provider[adapter.name]
        con.execute(
            "INSERT INTO ingest_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_at, adapter.name, ", ".join(str(p) for p in adapter.source_files()),
                status.get(adapter.name, "ok"), s.records, int(loaded.get(adapter.name, 0)),
                s.skipped_lines + s.skipped_events, s.duplicates,
                ", ".join(sorted(m.split(":", 1)[1] for m in s.unknown_models)) or None,
            ],
        )


def _build_views(con: duckdb.DuckDBPyConnection) -> None:
    """Create the enriched view every dashboard query builds on."""
    con.execute("DROP VIEW IF EXISTS events_enriched")
    con.execute(
        """
        CREATE VIEW events_enriched AS
        SELECT
            e.*,
            COALESCE(pl.provider_label, e.provider) AS provider_label,
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
            -- COALESCE per term: one NULL term (e.g. cache_creation_tokens, which only
            -- Claude Code reports) must not NULL the whole sum. reasoning_tokens is a
            -- subset of output_tokens, so it is deliberately not added.
            (COALESCE(e.input_tokens, 0) + COALESCE(e.output_tokens, 0)
                + COALESCE(e.cache_read_tokens, 0) + COALESCE(e.cache_creation_tokens, 0))
                AS total_tokens
        FROM events e
        LEFT JOIN employees emp ON e.user_email = emp.email
        LEFT JOIN provider_labels pl ON e.provider = pl.provider
        """
    )


# --- orchestration ------------------------------------------------------------

def _resolve_paths(
    jsonl_path: Path | None, provider_paths: dict[str, dict[str, Path | str]] | None
) -> dict[str, dict[str, Path | str]]:
    """Per-adapter path overrides.

    When the caller passes an explicit Claude Code file, the optional provider files are
    looked up *next to it* (same file names as the configured defaults), so
    ``build_database(tmp/telemetry.jsonl, ...)`` never silently mixes in the real
    dataset. Explicit ``provider_paths`` always win.
    """
    resolved: dict[str, dict[str, Path | str]] = {}
    if jsonl_path is not None:
        base = jsonl_path.parent
        resolved = {
            "claude_code": {"events": jsonl_path},
            "openai": {"events": base / config.OPENAI_USAGE_JSONL.name},
            "gemini": {"events": base / config.GEMINI_USAGE_JSONL.name},
            "deepseek": {"events": base / config.DEEPSEEK_USAGE_CSV.name},
        }
    for name, overrides in (provider_paths or {}).items():
        resolved.setdefault(name, {}).update(overrides)
    return resolved


def build_database(
    jsonl_path: Path | None = None,
    csv_path: Path | None = None,
    duckdb_path: Path | None = None,
    *,
    providers: list[str] | None = None,
    provider_paths: dict[str, dict[str, Path | str]] | None = None,
) -> IngestStats:
    """Full ETL. Returns aggregate ingest stats (``by_provider``, ``unknown_models``,
    ``duplicates`` included) for reporting/tests.

    ``providers`` defaults to ``config.PROVIDERS``; ``provider_paths`` overrides source
    files per provider, e.g. ``{"openai": {"events": "x.jsonl"}}``.
    """
    csv_path = csv_path or config.EMPLOYEES_CSV
    duckdb_path = duckdb_path or config.DUCKDB_PATH
    adapters = get_adapters(providers or config.PROVIDERS, _resolve_paths(jsonl_path, provider_paths))

    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    if duckdb_path.exists():
        duckdb_path.unlink()  # rebuild from scratch for deterministic results

    run_at = datetime.now(timezone.utc)
    print(f"→ Building {duckdb_path}  (providers: {', '.join(a.name for a in adapters)})")
    con = duckdb.connect(str(duckdb_path))
    try:
        stats, per_provider, status = _load_events(con, adapters)
        print("  • loading employee directory …")
        emp_count = _load_employees(con, csv_path)
        _load_provider_labels(con)

        print("  • de-duplicating …")
        for provider, n in _dedupe(con).items():
            per_provider[provider].duplicates = n
            stats.duplicates += n
        for provider, models in _unpriced_models(con).items():
            tagged = {f"{provider}:{m}" for m in models}
            per_provider[provider].unknown_models |= tagged
            stats.unknown_models |= tagged

        _write_ingest_runs(con, adapters, per_provider, status, run_at)
        print("  • building enriched view …")
        _build_views(con)
        summary = con.execute("""
            SELECT provider,
                   count(*)                                            AS rows,
                   count(*) FILTER (WHERE event_type = 'api_request') AS requests,
                   count(*) FILTER (WHERE event_type = 'api_error')   AS errors,
                   count(DISTINCT user_email)                          AS users,
                   COALESCE(sum(cost_usd), 0)                          AS cost
            FROM events GROUP BY provider ORDER BY cost DESC
        """).fetchall()
        con.execute("CHECKPOINT")
    finally:
        con.close()

    _print_summary(stats, emp_count, per_provider, status, summary)
    return stats


def _print_summary(stats, emp_count, per_provider, status, summary) -> None:
    print("\n=== ETL summary ===")
    print(f"  batches parsed        : {stats.batches:,}")
    print(f"  events loaded         : {stats.events - stats.duplicates:,}")
    print(f"  employees loaded      : {emp_count:,}")
    print(f"  skipped (bad lines)   : {stats.skipped_lines:,}")
    print(f"  skipped (bad events)  : {stats.skipped_events:,}")
    print(f"  duplicates removed    : {stats.duplicates:,}")
    for etype, count in sorted(stats.by_event_type.items(), key=lambda kv: -kv[1]):
        print(f"    - {etype:<16}: {count:,}")

    print("\n  per provider:")
    print(f"    {'provider':<12} {'rows':>9} {'requests':>9} {'errors':>7} {'users':>6} {'cost_usd':>11}")
    for provider, rows, requests, errors, users, cost in summary:
        print(f"    {provider:<12} {rows:>9,} {requests:>9,} {errors:>7,} {users:>6,} {cost:>11,.2f}")
    for provider, st in status.items():
        if st != "ok":
            print(f"    {provider:<12} ({st})")
    for provider, s in per_provider.items():
        if s.unknown_models:
            names = ", ".join(sorted(m.split(":", 1)[1] for m in s.unknown_models))
            print(f"  ! {provider}: requests without a cost (left NULL): {names}")
    if stats.errors:
        print(f"  first {min(len(stats.errors), 5)} parse warnings:")
        for msg in stats.errors[:5]:
            print(f"    ! {msg}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the DuckDB analytics database.")
    parser.add_argument("--jsonl", type=Path, default=None, help="telemetry_logs.jsonl path")
    parser.add_argument("--csv", type=Path, default=None, help="employees.csv path")
    parser.add_argument("--db", type=Path, default=None, help="output DuckDB path")
    parser.add_argument("--providers", default=None,
                        help="comma-separated providers to load (default: $PROVIDERS / all)")
    args = parser.parse_args(argv)
    providers = [p.strip() for p in args.providers.split(",") if p.strip()] if args.providers else None
    try:
        build_database(args.jsonl, args.csv, args.db, providers=providers)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
