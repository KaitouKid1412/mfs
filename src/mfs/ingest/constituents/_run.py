"""Orchestration for NSE Index Constituents ingestion (Phase 2.2.B)."""

from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path
from typing import Iterable

import polars as pl

from mfs import paths
from mfs.db import writers as w
from mfs.errors import IngestError
from mfs.schemas import IndexConstituent
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_YM_RE = re.compile(r"^(\d{4})-(\d{2})$")

# D9 [REVIEW-EXTENDED]: tickers derived (and ingested) on top of the
# benchmark_ticker column of configs/benchmarks.csv. NIFTY 50 TRI is the
# equity sleeve of the three hybrid benchmarks and NIFTY Bank TRI is the
# banking benchmark candidate — both have liquid Direct+Growth index-fund
# trackers in holdings_monthly (verified live 2026-06-13), and both already
# have TRI series in benchmark_daily. They are not (yet) any category's
# mapped benchmark, so discover_tickers would otherwise never ingest the
# CSVs derive.py writes for them.
_EXTRA_TICKERS: list[str] = ["NIFTY 50 TRI", "NIFTY Bank TRI"]


def _parse_ym(stem: str) -> date | None:
    m = _YM_RE.match(stem)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return None


def _read_manual_csv(path: Path) -> list[dict]:
    """Read a manual constituent CSV. Returns list of dict rows.

    Validates: header has isin + weight_pct columns. Drops rows with empty
    ISIN or non-numeric weight. Logs the row count for traceability.
    """
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "isin" not in reader.fieldnames:
            raise IngestError(
                f"Constituent CSV {path} is missing an 'isin' header column. "
                f"Found columns: {reader.fieldnames}"
            )
        if "weight_pct" not in reader.fieldnames:
            raise IngestError(
                f"Constituent CSV {path} is missing a 'weight_pct' header column."
            )
        for raw in reader:
            isin = (raw.get("isin") or "").strip()
            if not isin:
                continue
            w_raw = (raw.get("weight_pct") or "").strip().rstrip("%")
            try:
                w_val = float(w_raw)
            except ValueError:
                continue
            rows.append({
                "isin": isin,
                "weight_pct": w_val,
                "security_name": (raw.get("security_name") or "").strip() or None,
            })
    return rows


def _records_for_ticker(ticker: str) -> Iterable[IndexConstituent]:
    """Walk the manual directory for one ticker, yielding IndexConstituent
    records across all available months."""
    slug = paths.ticker_slug(ticker)
    base = paths.index_constituents_manual_dir(slug)
    if not base.exists():
        return
    for f in sorted(base.glob("*.csv")):
        ym = _parse_ym(f.stem)
        if ym is None:
            log.warning(
                "constituents.skip_unparseable_filename",
                ticker=ticker, file=str(f),
            )
            continue
        for row in _read_manual_csv(f):
            yield IndexConstituent(
                ticker=ticker,
                isin=row["isin"],
                as_of_month=ym,
                weight_pct=row["weight_pct"],
                security_name=row["security_name"],
            )


def run_for_ticker(ticker: str) -> dict:
    """Ingest all monthly CSVs for one benchmark ticker. Returns a summary."""
    records = list(_records_for_ticker(ticker))
    if not records:
        log.info("constituents.no_manual_csvs", ticker=ticker)
        return {"ticker": ticker, "rows_written": 0, "n_months": 0}
    df = pl.DataFrame([r.model_dump() for r in records])
    n = w.upsert_index_constituents(df)
    months = sorted(set(r.as_of_month for r in records))
    log.info(
        "constituents.ingested",
        ticker=ticker, rows=n, n_months=len(months),
        first_month=str(months[0]), last_month=str(months[-1]),
    )
    return {
        "ticker": ticker,
        "rows_written": n,
        "n_months": len(months),
        "months": [m.isoformat() for m in months],
    }


def discover_tickers() -> list[str]:
    """Return benchmark tickers that have at least one manual CSV present.

    Walks data/raw/index_constituents/manual/<slug>/. Returns canonical
    ticker names by reverse-mapping the slug from configs/benchmarks.csv.
    """
    base = paths.raw_dir() / "index_constituents" / "manual"
    if not base.exists():
        return []
    # Read configs/benchmarks.csv to know all tickers
    from mfs.config import get_settings
    bench_csv = get_settings().benchmarks_csv
    tickers: list[str] = []
    seen: set[str] = set()
    if bench_csv.exists():
        with open(bench_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # D5: benchmarks.csv carries '#'-prefixed comment rows.
                if (row.get("canonical_category") or "").lstrip().startswith("#"):
                    continue
                t = (row.get("benchmark_ticker") or "").strip()
                if t and t not in seen:
                    seen.add(t)
                    tickers.append(t)
    for t in _EXTRA_TICKERS:
        if t not in seen:
            seen.add(t)
            tickers.append(t)
    # Keep only those with a directory present
    out: list[str] = []
    for t in tickers:
        slug = paths.ticker_slug(t)
        if (base / slug).exists():
            out.append(t)
    return out


def run_all() -> dict[str, dict]:
    """Run all tickers that have at least one manual CSV present."""
    tickers = discover_tickers()
    if not tickers:
        log.info("constituents.no_tickers_with_manual_csvs")
        return {}
    return {t: run_for_ticker(t) for t in tickers}
