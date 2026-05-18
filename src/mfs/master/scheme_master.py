"""Build the canonical scheme dimension from the latest AMFI NAVAll + nav_daily history.

For each (scheme_code) we derive:
- plan_type: DIRECT | REGULAR | UNKNOWN
- option_type: GROWTH | IDCW | UNKNOWN
- amc_code: a normalized slug of amc_name
- canonical_category: human category (Large Cap, Mid Cap, …)
- benchmark_ticker: from configs/benchmarks.csv
- base_fund_id: shared key across Direct/Regular and Growth/IDCW siblings
- inception_date: first NAV date in history
- is_active: present in the latest NAV pull
- last_seen_date: max NAV date observed
"""

from __future__ import annotations

import re
from datetime import date

import polars as pl

from mfs import paths
from mfs.ingest import amfi_nav
from mfs.io.parquet import read_dataset, write_single
from mfs.master.benchmark_map import benchmark_for
from mfs.utils.logging import get_logger

log = get_logger(__name__)

# Map AMFI category strings to our canonical names. Match by substring (case-insensitive).
CATEGORY_RULES: list[tuple[str, str]] = [
    ("Large Cap", "Large Cap"),
    ("Large & Mid", "Large & Mid Cap"),
    ("Large and Mid", "Large & Mid Cap"),
    ("Mid Cap", "Mid Cap"),
    ("Small Cap", "Small Cap"),
    ("Multi Cap", "Multi Cap"),
    ("Flexi Cap", "Flexi Cap"),
    ("Focused", "Focused"),
    ("ELSS", "ELSS"),
    ("Equity Linked Savings", "ELSS"),
    ("Tax Saver", "ELSS"),
    ("Value", "Value"),
    ("Contra", "Contra"),
    ("Dividend Yield", "Dividend Yield"),
    ("Aggressive Hybrid", "Aggressive Hybrid"),
    ("Balanced Advantage", "Balanced Advantage"),
    ("Dynamic Asset Allocation", "Balanced Advantage"),
    ("Equity Savings", "Equity Savings"),
    ("Sector", "Sectoral/Thematic"),
    ("Thematic", "Sectoral/Thematic"),
]


def _amc_slug(s: str) -> str:
    s = re.sub(r"\bMutual Fund\b", "", s, flags=re.IGNORECASE).strip()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _classify_plan_option(scheme_name: str) -> tuple[str, str]:
    name_lc = scheme_name.lower()
    plan = "UNKNOWN"
    if re.search(r"\bdirect( plan)?\b", name_lc):
        plan = "DIRECT"
    elif re.search(r"\bregular( plan)?\b", name_lc):
        plan = "REGULAR"
    elif "direct" in name_lc:
        plan = "DIRECT"
    elif "regular" in name_lc:
        plan = "REGULAR"
    option = "UNKNOWN"
    if "idcw" in name_lc or "dividend" in name_lc:
        option = "IDCW"
    elif "growth" in name_lc:
        option = "GROWTH"
    return plan, option


def _canonical_category(raw: str | None) -> str | None:
    if not raw:
        return None
    for needle, canon in CATEGORY_RULES:
        if needle.lower() in raw.lower():
            return canon
    return None


def _base_fund_id(scheme_name: str, amc_slug: str) -> str:
    """Strip plan/option tokens to derive a shared id across siblings."""
    s = scheme_name
    s = re.sub(r"\b(direct|regular)( plan)?\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(growth|idcw|dividend|reinvestment|payout)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(plan|option)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return f"{amc_slug}::{re.sub(r'[^a-z0-9]+', '_', s).strip('_')}"


def build() -> pl.DataFrame:
    """Pull today's AMFI snapshot, derive scheme master rows, persist to curated."""
    today = date.today()
    content = amfi_nav.fetch_today().decode("utf-8", errors="replace")
    snap = amfi_nav.parse_to_dataframe(content)
    if snap.is_empty():
        raise RuntimeError("Empty AMFI NAVAll snapshot; cannot build scheme master")

    # Derive plan/option/category/amc_slug/base_fund_id per row
    snap_pd = snap.unique("scheme_code", keep="last")
    rows = []
    for r in snap_pd.iter_rows(named=True):
        scheme_name = r["scheme_name"] or ""
        amc_name = r["amc_name"] or "Unknown"
        plan, option = _classify_plan_option(scheme_name)
        canon = _canonical_category(r["amfi_category"])
        amc_code = _amc_slug(amc_name)
        rows.append(
            {
                "scheme_code": r["scheme_code"],
                "isin_growth": r["isin_growth"],
                "isin_idcw": r["isin_idcw"],
                "scheme_name": scheme_name,
                "amc_name": amc_name,
                "amc_code": amc_code,
                "plan_type": plan,
                "option_type": option,
                "amfi_category": r["amfi_category"],
                "canonical_category": canon,
                "benchmark_ticker": benchmark_for(canon),
                "inception_date": None,
                "base_fund_id": _base_fund_id(scheme_name, amc_code),
                "is_active": True,
                "last_seen_date": today,
            }
        )

    df = pl.DataFrame(rows)

    # Compute inception_date and last_seen_date from nav_daily history (if present)
    navs = read_dataset(paths.nav_daily_dataset())
    if not navs.is_empty():
        bounds = navs.group_by("scheme_code").agg(
            pl.col("nav_date").min().alias("inception_date"),
            pl.col("nav_date").max().alias("last_seen_date_hist"),
        )
        df = df.join(bounds, on="scheme_code", how="left")
        df = df.with_columns(
            pl.coalesce(["inception_date_right", "inception_date"]).alias("inception_date"),
            pl.coalesce(["last_seen_date_hist", "last_seen_date"]).alias("last_seen_date"),
        )
        if "inception_date_right" in df.columns:
            df = df.drop("inception_date_right")
        if "last_seen_date_hist" in df.columns:
            df = df.drop("last_seen_date_hist")

    df = df.with_columns(
        pl.col("scheme_code").cast(pl.Utf8),
        pl.col("inception_date").cast(pl.Date),
        pl.col("last_seen_date").cast(pl.Date),
    )

    write_single(df, paths.scheme_master_file())
    log.info("scheme_master.built", rows=df.height)
    return df
