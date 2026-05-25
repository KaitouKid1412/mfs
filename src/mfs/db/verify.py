"""mfs db verify — cross-check parquet vs Postgres counts and bounds.

Run after `mfs db migrate` and BEFORE deleting any parquet files. The verify
step is the gate between "data in two places" (safe) and "parquet deleted"
(point of no return).
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl
import psycopg

from mfs import paths
from mfs.db.connection import connect
from mfs.io.parquet import read_dataset
from mfs.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class VerifyRow:
    table: str
    parquet_rows: int
    db_rows: int
    parquet_min: str
    db_min: str
    parquet_max: str
    db_max: str
    ok: bool

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        flag = "OK " if self.ok else "FAIL"
        return (
            f"  [{flag}] {self.table:20s}  pq={self.parquet_rows:>10}  "
            f"db={self.db_rows:>10}  pq=[{self.parquet_min}..{self.parquet_max}]  "
            f"db=[{self.db_min}..{self.db_max}]"
        )


def _pq_bounds(df: pl.DataFrame, date_col: str) -> tuple[int, str, str]:
    if df.is_empty():
        return 0, "-", "-"
    return df.height, str(df[date_col].min()), str(df[date_col].max())


def _db_bounds(conn: psycopg.Connection, table: str, date_col: str) -> tuple[int, str, str]:
    row = conn.execute(
        f"SELECT COUNT(*), MIN({date_col}), MAX({date_col}) FROM {table}"
    ).fetchone()
    n, mn, mx = row
    return n, str(mn) if mn else "-", str(mx) if mx else "-"


def verify_all() -> list[VerifyRow]:
    results: list[VerifyRow] = []
    with connect() as c:
        # nav_daily
        pq = read_dataset(paths.nav_daily_dataset())
        pq_n, pq_min, pq_max = _pq_bounds(pq, "nav_date")
        db_n, db_min, db_max = _db_bounds(c, "nav_daily", "nav_date")
        results.append(VerifyRow(
            "nav_daily", pq_n, db_n, pq_min, db_min, pq_max, db_max,
            ok=(pq_n == db_n and pq_min == db_min and pq_max == db_max),
        ))

        # benchmark_daily — read per-partition because synthetic hybrid
        # partitions add an `is_synthetic` column the others lack
        bench_root = paths.benchmark_daily_dataset()
        bench_total = 0
        bench_min = None
        bench_max = None
        if bench_root.exists():
            for part_dir in bench_root.iterdir():
                data = part_dir / "data.parquet"
                if not data.exists():
                    continue
                pq = pl.read_parquet(data, columns=["date"])
                if pq.is_empty():
                    continue
                bench_total += pq.height
                d_min, d_max = pq["date"].min(), pq["date"].max()
                bench_min = d_min if bench_min is None else min(bench_min, d_min)
                bench_max = d_max if bench_max is None else max(bench_max, d_max)
        db_n, db_min, db_max = _db_bounds(c, "benchmark_daily", "date")
        results.append(VerifyRow(
            "benchmark_daily", bench_total, db_n,
            str(bench_min) if bench_min else "-", db_min,
            str(bench_max) if bench_max else "-", db_max,
            ok=(bench_total == db_n
                and str(bench_min) == db_min and str(bench_max) == db_max),
        ))

        # risk_free_daily
        rf_path = paths.risk_free_daily_file()
        pq_n, pq_min, pq_max = (
            _pq_bounds(pl.read_parquet(rf_path), "date") if rf_path.exists() else (0, "-", "-")
        )
        db_n, db_min, db_max = _db_bounds(c, "risk_free_daily", "date")
        results.append(VerifyRow(
            "risk_free_daily", pq_n, db_n, pq_min, db_min, pq_max, db_max,
            ok=(pq_n == db_n and pq_min == db_min and pq_max == db_max),
        ))

        # scheme_master (no obvious date column — use last_seen_date for range)
        sm_path = paths.scheme_master_file()
        pq = pl.read_parquet(sm_path) if sm_path.exists() else pl.DataFrame()
        pq_n, pq_min, pq_max = _pq_bounds(pq, "last_seen_date")
        db_n, db_min, db_max = _db_bounds(c, "scheme_master", "last_seen_date")
        results.append(VerifyRow(
            "scheme_master", pq_n, db_n, pq_min, db_min, pq_max, db_max,
            ok=(pq_n == db_n),  # date bounds can wobble by 1 day if rebuilt at midnight
        ))

        # computed_metrics
        pq = read_dataset(paths.computed_metrics_dataset())
        pq_n, pq_min, pq_max = _pq_bounds(pq, "as_of_date")
        db_n, db_min, db_max = _db_bounds(c, "computed_metrics", "as_of_date")
        results.append(VerifyRow(
            "computed_metrics", pq_n, db_n, pq_min, db_min, pq_max, db_max,
            ok=(pq_n == db_n and pq_min == db_min and pq_max == db_max),
        ))

    for r in results:
        log.info("db.verify", table=r.table, ok=r.ok,
                 pq=r.parquet_rows, db=r.db_rows)
    return results
