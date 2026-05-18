from __future__ import annotations

from pathlib import Path

import polars as pl


def write_partition(df: pl.DataFrame, dataset_dir: Path, partition_value: str, partition_key: str) -> Path:
    """Write df to {dataset_dir}/{partition_key}={partition_value}/data.parquet atomically."""
    part_dir = dataset_dir / f"{partition_key}={partition_value}"
    part_dir.mkdir(parents=True, exist_ok=True)
    final = part_dir / "data.parquet"
    tmp = part_dir / "data.parquet.tmp"
    df.write_parquet(tmp, compression="zstd")
    tmp.replace(final)
    return final


def write_single(df: pl.DataFrame, file_path: Path) -> Path:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    df.write_parquet(tmp, compression="zstd")
    tmp.replace(file_path)
    return file_path


def read_dataset(dataset_dir: Path) -> pl.DataFrame:
    if not dataset_dir.exists():
        return pl.DataFrame()
    files = list(dataset_dir.rglob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.read_parquet(files, hive_partitioning=True)


def read_partition(dataset_dir: Path, partition_value: str, partition_key: str) -> pl.DataFrame:
    p = dataset_dir / f"{partition_key}={partition_value}" / "data.parquet"
    if not p.exists():
        return pl.DataFrame()
    return pl.read_parquet(p)
