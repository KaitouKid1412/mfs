"""NSE daily bhavcopy ingestion (Phase 2.3.B).

Downloads `sec_bhavdata_full_DDMMYYYY.csv` from
`https://archives.nseindia.com/products/content/`. For each row with
SERIES='EQ' (cash-market equity), writes one (isin, date, close,
total_traded_value) row to `stock_adv_daily`.

Symbol→ISIN mapping comes from NSE's EQUITY_L.csv (re-cached on every run).

The endpoint sometimes 403s without warning; per the brittleness audit
(Tier 2.6), we retry transient HTTP errors via the existing io/http layer
and treat per-day 404s as "non-trading day, skip" rather than fatal.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl

from mfs import paths
from mfs.db import writers as w
from mfs.io.http import TransientHttpError
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_BHAVCOPY_URL = (
    "https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
)
_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

# A browser-style header set — NSE archives works without anti-bot for these
# static-file paths, but a real UA reduces the chance of a server-side filter
# kicking in on a future change.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
}


def _fetch_with_retry(url: str) -> bytes | None:
    """GET a URL; return bytes on 200, None on 404, raise on other errors.

    Returns None specifically for 404 because non-trading days return 404
    and shouldn't halt the multi-day walk.
    """
    try:
        with httpx.Client(headers=_HEADERS, timeout=30, follow_redirects=True) as c:
            r = c.get(url)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
        raise TransientHttpError(str(e)) from e
    if r.status_code == 404:
        return None
    if r.status_code in (429, 500, 502, 503, 504):
        raise TransientHttpError(f"{r.status_code} for {url}")
    r.raise_for_status()
    return r.content


def fetch_equity_list() -> dict[str, str]:
    """Return {symbol: isin} from NSE EQUITY_L.csv. Caches on disk."""
    out_path = paths.nse_equity_list_raw()
    if out_path.exists():
        data = out_path.read_bytes()
    else:
        log.info("bhavcopy.equity_list.fetch")
        data = _fetch_with_retry(_EQUITY_LIST_URL)
        if data is None:
            raise RuntimeError("NSE EQUITY_L.csv unavailable (404)")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
    reader = csv.reader(io.StringIO(data.decode("utf-8")))
    header = next(reader)
    sym_idx = next(i for i, h in enumerate(header) if h.strip() == "SYMBOL")
    isin_idx = next(i for i, h in enumerate(header) if "ISIN" in h)
    out: dict[str, str] = {}
    for row in reader:
        if len(row) <= max(sym_idx, isin_idx):
            continue
        sym = row[sym_idx].strip()
        isin = row[isin_idx].strip()
        if sym and isin:
            out[sym] = isin
    log.info("bhavcopy.equity_list.loaded", n=len(out))
    return out


def fetch_one(d: date, symbol_to_isin: dict[str, str] | None = None) -> int:
    """Fetch + parse + upsert one day's bhavcopy. Returns rows ingested.

    Non-trading days (404) return 0 without raising. Re-running for a date
    already present in stock_adv_daily is idempotent on the (isin, date) PK.
    """
    if symbol_to_isin is None:
        symbol_to_isin = fetch_equity_list()
    cached = paths.bhavcopy_raw(d)
    if cached.exists():
        data = cached.read_bytes()
    else:
        url = _BHAVCOPY_URL.format(ddmmyyyy=d.strftime("%d%m%Y"))
        log.info("bhavcopy.fetch", date=d.isoformat(), url=url)
        data = _fetch_with_retry(url)
        if data is None:
            log.info("bhavcopy.non_trading_day", date=d.isoformat())
            return 0
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)

    rows = _parse_bhavcopy(data, d, symbol_to_isin)
    if not rows:
        log.warning("bhavcopy.empty_after_parse", date=d.isoformat())
        return 0
    df = pl.DataFrame(rows)
    n = w.upsert_stock_adv(df)
    log.info("bhavcopy.ingested", date=d.isoformat(), rows=n)
    return n


def _parse_bhavcopy(
    data: bytes, d: date, symbol_to_isin: dict[str, str],
) -> list[dict]:
    """Parse the CSV bytes into (isin, symbol, date, close, ttv_INR) rows.

    TURNOVER_LACS is in Lakhs (1 Lac = 100,000 INR), converted to INR here.
    """
    text = data.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = [h.strip() for h in next(reader)]
    idx = {col: i for i, col in enumerate(header)}
    needed = ["SYMBOL", "SERIES", "CLOSE_PRICE", "TURNOVER_LACS"]
    for col in needed:
        if col not in idx:
            log.warning("bhavcopy.unexpected_header", header=header)
            return []
    rows: list[dict] = []
    for raw in reader:
        if len(raw) <= idx["TURNOVER_LACS"]:
            continue
        series = raw[idx["SERIES"]].strip()
        if series != "EQ":
            continue
        symbol = raw[idx["SYMBOL"]].strip()
        isin = symbol_to_isin.get(symbol)
        if not isin:
            continue  # unknown symbol — skip, equity list will refresh weekly
        try:
            close = float(raw[idx["CLOSE_PRICE"]])
        except ValueError:
            close = None
        try:
            ttv_lacs = float(raw[idx["TURNOVER_LACS"]])
        except ValueError:
            continue
        rows.append({
            "isin": isin,
            "symbol": symbol,
            "date": d,
            "close": close,
            "total_traded_value": ttv_lacs * 100_000.0,  # Lacs → INR
        })
    return rows


def ingest_recent(n_days: int = 75) -> dict:
    """Walk the most recent n_days calendar days, ingesting each trading day.

    Default 75 days covers the 60-day median window with slack for holidays.
    Already-cached files are reused.
    """
    today = date.today()
    symbol_to_isin = fetch_equity_list()
    fetched = 0
    rows = 0
    for offset in range(1, n_days + 1):
        d = today - timedelta(days=offset)
        # Skip weekends (NSE is closed Sat/Sun).
        if d.weekday() >= 5:
            continue
        n = fetch_one(d, symbol_to_isin=symbol_to_isin)
        rows += n
        if n > 0:
            fetched += 1
    log.info("bhavcopy.recent.done", days_with_data=fetched, total_rows=rows)
    return {"days_with_data": fetched, "total_rows": rows}
