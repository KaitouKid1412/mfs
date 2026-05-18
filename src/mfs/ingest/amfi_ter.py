"""AMFI Total Expense Ratio ingestion (stub for v1).

AMFI publishes daily TER tables. The exact CSV endpoint and headers change; for v1
we expose a minimal interface that reads user-provided CSVs at
data/raw/amfi_ter/manual/*.csv with columns: scheme_code, effective_date, ter_pct,
plan_type.

This stays optional in v1 — TER is currently used only as a tie-breaker and a data-
quality cross-check, neither of which gates the v1 ranking output.
"""

from __future__ import annotations

import polars as pl

from mfs import paths
from mfs.io.parquet import write_partition
from mfs.utils.logging import get_logger

log = get_logger(__name__)


def ingest_from_manual_csvs() -> int:
    manual_dir = paths.raw_dir() / "amfi_ter" / "manual"
    if not manual_dir.exists():
        return 0
    parts = []
    for csv in sorted(manual_dir.glob("*.csv")):
        try:
            df = pl.read_csv(csv)
        except Exception as e:  # noqa: BLE001
            log.warning("ter.manual.parse_failed", file=str(csv), err=str(e))
            continue
        df.columns = [c.strip().lower() for c in df.columns]
        required = {"scheme_code", "effective_date", "ter_pct"}
        if not required.issubset(set(df.columns)):
            continue
        df = df.with_columns(
            pl.col("effective_date").str.to_date(strict=False).alias("effective_date"),
            pl.col("scheme_code").cast(pl.Utf8),
            pl.col("ter_pct").cast(pl.Float64),
        )
        parts.append(df)
    if not parts:
        return 0
    all_rates = pl.concat(parts, how="diagonal_relaxed").unique(
        ["scheme_code", "effective_date"], keep="last"
    )
    by_year = all_rates.with_columns(pl.col("effective_date").dt.year().alias("year"))
    for year, chunk in by_year.group_by("year"):
        write_partition(
            chunk.drop("year"),
            paths.ter_daily_dataset(),
            str(int(year[0])),
            "year",
        )
    log.info("ter.manual.ingested", rows=all_rates.height)
    return all_rates.height
