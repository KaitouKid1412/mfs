from __future__ import annotations

from pathlib import Path

from mfs.config import get_settings


def data_dir() -> Path:
    return get_settings().data_dir


def raw_dir() -> Path:
    return data_dir() / "raw"


def curated_dir() -> Path:
    return data_dir() / "curated"


def metrics_dir() -> Path:
    return data_dir() / "metrics"


def output_dir() -> Path:
    return data_dir() / "output"


def amfi_nav_raw(year: int, month: int, day: int) -> Path:
    return raw_dir() / "amfi_nav" / f"{year:04d}" / f"{month:02d}" / f"{day:02d}" / "NAVAll.txt"


def amfi_history_raw(from_date: str, to_date: str) -> Path:
    return raw_dir() / "amfi_nav_history" / f"{from_date}_{to_date}.txt"


def benchmark_raw(ticker_slug: str, date_str: str) -> Path:
    return raw_dir() / "benchmarks" / ticker_slug / f"{date_str}.csv"


def fbil_raw(year: int, month: int) -> Path:
    return raw_dir() / "fbil_tbill" / f"{year:04d}-{month:02d}.csv"


def rbi_press_release_raw(prid: int) -> Path:
    return raw_dir() / "fbil_tbill" / "rbi_press_releases" / f"prid_{prid}.html"


def rbi_tbill_state_file() -> Path:
    return raw_dir() / "fbil_tbill" / "rbi_state.json"


def nav_daily_dataset() -> Path:
    return curated_dir() / "nav_daily"


def benchmark_daily_dataset() -> Path:
    return curated_dir() / "benchmark_daily"


def risk_free_daily_file() -> Path:
    return curated_dir() / "risk_free_daily" / "risk_free.parquet"


def scheme_master_file() -> Path:
    return curated_dir() / "scheme_master" / "scheme_master.parquet"


def ter_daily_dataset() -> Path:
    return curated_dir() / "ter_daily"


def computed_metrics_dataset() -> Path:
    return metrics_dir() / "computed_metrics"


def shortlist_dir(as_of: str) -> Path:
    return output_dir() / "shortlist" / as_of


def ensure_parents(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p
