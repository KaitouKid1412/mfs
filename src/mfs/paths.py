from __future__ import annotations

from datetime import date
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


def factsheet_raw(amc_slug: str, ym: str) -> Path:
    """Archive location for one AMC's monthly factsheet PDF.

    `ym` is the data month (YYYY-MM, e.g. '2026-04'), not the publish month —
    HDFC's "April 2026 factsheet" gets stored at hdfc/2026-04.pdf even though
    HDFC publishes it in May.
    """
    return raw_dir() / "factsheets" / amc_slug / f"{ym}.pdf"


def annual_report_raw(amc_slug: str, fy_label: str) -> Path:
    """Archive location for one AMC's (abridged) annual report PDF.

    `fy_label` is the Indian fiscal year the report covers (e.g. '2023-24').
    Used by AMCs whose factsheet omits a metric SEBI mandates only in the
    annual report (e.g. quant, which deliberately omits Portfolio Turnover
    Ratio from its monthly factsheet).
    """
    return raw_dir() / "annual_reports" / amc_slug / f"{fy_label}.pdf"


def bhavcopy_raw(d: date) -> Path:
    """Archive location for a single day's NSE bhavcopy CSV."""
    return raw_dir() / "bhavcopy" / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}.csv"


def nse_equity_list_raw() -> Path:
    """Latest snapshot of NSE EQUITY_L.csv (symbol → ISIN master)."""
    return raw_dir() / "bhavcopy" / "equity_list.csv"


def index_constituents_manual_dir(ticker_slug: str) -> Path:
    """User-provided monthly weight CSVs for one benchmark ticker.

    NSE doesn't publish constituent weights as a machine-readable file
    (only membership lists). For Phase 2.2.B we expect monthly weight CSVs
    here with columns: isin, weight_pct, security_name (header row).

    Example: data/raw/index_constituents/manual/nifty_100_tri/2026-04.csv
    """
    return raw_dir() / "index_constituents" / "manual" / ticker_slug


def holdings_excel_raw(amc_slug: str, ym: str, scheme_filename: str) -> Path:
    """Cached per-scheme monthly portfolio Excel (Phase 3.C).

    Phase 3.C sources holdings from each AMC's own monthly portfolio
    disclosure Excels (one Excel per scheme), as opposed to factsheet PDFs.
    Files are cached on disk so repeat runs of an AMC don't re-download.

    Layout: data/raw/holdings/<amc>/<YYYY-MM>/<scheme_filename>.xlsx
    """
    safe = scheme_filename.replace("/", "_")
    return raw_dir() / "holdings" / amc_slug / ym / safe


def ticker_slug(ticker: str) -> str:
    """Lowercase, underscore-separated, alphanumeric-only ticker slug.

    'NIFTY 100 TRI' -> 'nifty_100_tri'. Used for filesystem paths so we don't
    have spaces or special chars in directory names.
    """
    import re
    s = re.sub(r"[^a-z0-9]+", "_", ticker.lower()).strip("_")
    return s


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
