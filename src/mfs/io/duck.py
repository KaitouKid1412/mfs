from __future__ import annotations

import duckdb

from mfs import paths


def connect() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB connection with views registered over the curated Parquet datasets."""
    con = duckdb.connect(":memory:")
    register_views(con)
    return con


def register_views(con: duckdb.DuckDBPyConnection) -> None:
    nav_glob = paths.nav_daily_dataset() / "**" / "*.parquet"
    bench_glob = paths.benchmark_daily_dataset() / "**" / "*.parquet"
    rf_file = paths.risk_free_daily_file()
    sm_file = paths.scheme_master_file()
    cm_glob = paths.computed_metrics_dataset() / "**" / "*.parquet"

    if any(paths.nav_daily_dataset().rglob("*.parquet")):
        con.execute(
            f"CREATE OR REPLACE VIEW nav_daily AS SELECT * FROM read_parquet('{nav_glob}', hive_partitioning=true)"
        )
    if any(paths.benchmark_daily_dataset().rglob("*.parquet")):
        con.execute(
            f"CREATE OR REPLACE VIEW benchmark_daily AS SELECT * FROM read_parquet('{bench_glob}', hive_partitioning=true)"
        )
    if rf_file.exists():
        con.execute(f"CREATE OR REPLACE VIEW risk_free_daily AS SELECT * FROM read_parquet('{rf_file}')")
    if sm_file.exists():
        con.execute(f"CREATE OR REPLACE VIEW scheme_master AS SELECT * FROM read_parquet('{sm_file}')")
    if paths.computed_metrics_dataset().exists() and any(paths.computed_metrics_dataset().rglob("*.parquet")):
        con.execute(
            f"CREATE OR REPLACE VIEW computed_metrics AS SELECT * FROM read_parquet('{cm_glob}', hive_partitioning=true)"
        )
