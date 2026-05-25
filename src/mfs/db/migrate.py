"""mfs db migrate — one-shot copy of parquet datasets into Postgres tables.

Strategy per table:
  1. Read parquet via polars (streaming for the big ones)
  2. COPY ... FROM STDIN (binary) into a TEMP table
  3. INSERT ... ON CONFLICT DO UPDATE from the temp into the real table
  4. DROP the temp

Idempotent — re-running is safe; existing rows get updated by primary key.
"""

from __future__ import annotations

from typing import Sequence

import polars as pl
import psycopg
from psycopg import sql

from mfs import paths
from mfs.db.connection import connect
from mfs.io.parquet import read_dataset
from mfs.utils.logging import get_logger

log = get_logger(__name__)


def _copy_upsert(
    conn: psycopg.Connection,
    table: str,
    columns: Sequence[str],
    rows: list[tuple],
    pk: Sequence[str],
) -> int:
    """COPY rows into a session-temp table, then UPSERT into `table`. Returns
    rows affected. Drops the temp table explicitly so the same caller can invoke
    `_copy_upsert` multiple times for the same target table in one connection."""
    if not rows:
        return 0
    import uuid

    tmp = f"_load_{table}_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS)").format(
                sql.Identifier(tmp), sql.Identifier(table)
            )
        )
        try:
            col_idents = sql.SQL(",").join(sql.Identifier(c) for c in columns)
            with cur.copy(
                sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(tmp), col_idents)
            ) as cp:
                for row in rows:
                    cp.write_row(row)

            update_cols = [c for c in columns if c not in pk]
            if update_cols:
                set_clause = sql.SQL(", ").join(
                    sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                    for c in update_cols
                )
                conflict_action = sql.SQL("DO UPDATE SET ") + set_clause
            else:
                conflict_action = sql.SQL("DO NOTHING")

            cur.execute(
                sql.SQL(
                    "INSERT INTO {table} ({cols}) SELECT {cols} FROM {tmp} "
                    "ON CONFLICT ({pk}) {conflict}"
                ).format(
                    table=sql.Identifier(table),
                    cols=col_idents,
                    tmp=sql.Identifier(tmp),
                    pk=sql.SQL(",").join(sql.Identifier(c) for c in pk),
                    conflict=conflict_action,
                )
            )
            affected = cur.rowcount
        finally:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(tmp)))
    return affected


def migrate_nav_daily() -> int:
    log.info("migrate.nav_daily.start")
    df = read_dataset(paths.nav_daily_dataset())
    if df.is_empty():
        log.warning("migrate.nav_daily.empty")
        return 0
    cols = [
        "scheme_code", "nav_date", "nav", "isin_growth", "isin_idcw",
        "scheme_name", "amc_name", "amfi_category",
    ]
    df = df.select(cols)
    total = 0
    BATCH = 250_000
    with connect() as c:
        for i in range(0, df.height, BATCH):
            chunk = df.slice(i, BATCH)
            rows = list(chunk.iter_rows())
            n = _copy_upsert(c, "nav_daily", cols, rows, pk=("scheme_code", "nav_date"))
            total += n
            log.info("migrate.nav_daily.chunk", from_=i, n=len(rows), affected=n)
    log.info("migrate.nav_daily.done", total=total)
    return total


def migrate_benchmark_daily() -> int:
    """Read each ticker partition individually — some have `is_synthetic` and
    some don't (legacy equity partitions), so a naive `read_dataset` raises a
    SchemaError. Per-partition reads side-step that."""
    log.info("migrate.benchmark_daily.start")
    root = paths.benchmark_daily_dataset()
    if not root.exists():
        log.warning("migrate.benchmark_daily.missing")
        return 0
    cols = ["ticker", "date", "close", "is_synthetic"]
    total = 0
    with connect() as c:
        for part_dir in sorted(root.iterdir()):
            if not part_dir.is_dir() or not part_dir.name.startswith("ticker="):
                continue
            data = part_dir / "data.parquet"
            if not data.exists():
                continue
            df = pl.read_parquet(data)
            if "is_synthetic" not in df.columns:
                df = df.with_columns(pl.lit(False).alias("is_synthetic"))
            else:
                df = df.with_columns(pl.col("is_synthetic").fill_null(False))
            df = df.select(cols)
            rows = list(df.iter_rows())
            n = _copy_upsert(c, "benchmark_daily", cols, rows, pk=("ticker", "date"))
            total += n
            log.info("migrate.benchmark_daily.partition",
                     ticker=part_dir.name, rows=len(rows), affected=n)
    log.info("migrate.benchmark_daily.done", total=total)
    return total


def migrate_risk_free_daily() -> int:
    log.info("migrate.risk_free_daily.start")
    p = paths.risk_free_daily_file()
    if not p.exists():
        log.warning("migrate.risk_free_daily.missing")
        return 0
    df = pl.read_parquet(p).select(["date", "rate_annual", "rate_daily"])
    rows = list(df.iter_rows())
    with connect() as c:
        n = _copy_upsert(c, "risk_free_daily", ["date", "rate_annual", "rate_daily"],
                         rows, pk=("date",))
    log.info("migrate.risk_free_daily.done", n=n)
    return n


def migrate_scheme_master() -> int:
    log.info("migrate.scheme_master.start")
    p = paths.scheme_master_file()
    if not p.exists():
        log.warning("migrate.scheme_master.missing")
        return 0
    cols = [
        "scheme_code", "isin_growth", "isin_idcw", "scheme_name", "amc_name",
        "amc_code", "plan_type", "option_type", "amfi_category",
        "canonical_category", "benchmark_ticker", "inception_date",
        "base_fund_id", "is_active", "last_seen_date",
    ]
    df = pl.read_parquet(p).select(cols)
    rows = list(df.iter_rows())
    with connect() as c:
        n = _copy_upsert(c, "scheme_master", cols, rows, pk=("scheme_code",))
    log.info("migrate.scheme_master.done", n=n)
    return n


def migrate_computed_metrics() -> int:
    log.info("migrate.computed_metrics.start")
    df = read_dataset(paths.computed_metrics_dataset())
    if df.is_empty():
        log.warning("migrate.computed_metrics.empty")
        return 0
    cols = [
        "as_of_date", "scheme_code", "canonical_category", "benchmark_ticker",
        "ret_3y_median", "ret_3y_p25", "ret_5y_median", "ret_5y_p25",
        "alpha_3y_annualized", "alpha_3y_tstat",
        "sortino_3y",
        "info_ratio_3y",
        "capture_up", "capture_down", "capture_efficiency",
        "r_squared_3y", "beta_3y",
        # Phase 2 columns. Older parquet partitions won't have these; fill null.
        "beta_3y_std", "r_squared_3y_mean", "style_drift_3y",
        "active_share_median_1y", "ptr_latest", "aum_impact_cost_days",
        "data_quality_flag", "computed_at", "pipeline_version",
    ]
    for c in cols:
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    df = df.select(cols)
    rows = list(df.iter_rows())
    with connect() as conn:
        n = _copy_upsert(conn, "computed_metrics", cols, rows,
                         pk=("as_of_date", "scheme_code"))
    log.info("migrate.computed_metrics.done", n=n)
    return n


def migrate_all() -> dict[str, int]:
    return {
        "scheme_master":    migrate_scheme_master(),
        "benchmark_daily":  migrate_benchmark_daily(),
        "risk_free_daily":  migrate_risk_free_daily(),
        "nav_daily":        migrate_nav_daily(),
        "computed_metrics": migrate_computed_metrics(),
    }
