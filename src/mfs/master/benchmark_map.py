from __future__ import annotations

from functools import lru_cache

import polars as pl

from mfs.config import get_settings


@lru_cache(maxsize=1)
def load_benchmark_map() -> pl.DataFrame:
    """Returns a DataFrame with columns: canonical_category, benchmark_ticker, composite_recipe.

    Lines starting with ``#`` are comments (D5 keeps a dated remap audit
    trail directly in configs/benchmarks.csv).
    """
    csv_path = get_settings().benchmarks_csv
    df = pl.read_csv(csv_path, comment_prefix="#")
    df.columns = [c.strip() for c in df.columns]
    return df.with_columns(pl.col("canonical_category").cast(pl.Utf8))


def benchmark_for(category: str | None) -> str | None:
    if not category:
        return None
    bm = load_benchmark_map()
    hit = bm.filter(pl.col("canonical_category") == category)
    if hit.is_empty():
        return None
    return hit["benchmark_ticker"][0]
