"""NSE daily bhavcopy ingestion (Phase 2.3.B).

Downloads `sec_bhavdata_full_DDMMYYYY.csv` from
`https://archives.nseindia.com/products/content/`. For each row with
SERIES='EQ' (cash-market equity), writes one (isin, date, close,
total_traded_value) row to `stock_adv_daily`.

Symbol→ISIN mapping comes from NSE's EQUITY_L.csv, cached on disk and refreshed
weekly (a stale map silently drops symbols renamed by M&A / corporate actions).

The endpoint sometimes 403s without warning; per the brittleness audit
(Tier 2.6), we retry transient HTTP errors via the existing io/http layer
and treat per-day 404s as "non-trading day, skip" rather than fatal.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import polars as pl

from mfs import paths
from mfs.db import queries as q
from mfs.db import writers as w
from mfs.errors import IngestError
from mfs.io.atomic import atomic_write_bytes
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


# Refresh the symbol→ISIN cache when it is older than this. NSE renames tickers
# after M&A / corporate actions; a stale map silently drops the renamed symbol's
# rows in _parse_bhavcopy, so we re-pull weekly to let those rows self-heal.
_EQUITY_LIST_MAX_AGE_DAYS = 7


def _equity_list_cache_fresh(path: Path, max_age_days: int) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) <= max_age_days * 86400


def fetch_equity_list(max_age_days: int = _EQUITY_LIST_MAX_AGE_DAYS) -> dict[str, str]:
    """Return {symbol: isin} from NSE EQUITY_L.csv.

    Cached on disk and refreshed weekly: if the cache is missing or older than
    ``max_age_days``, re-pull it. A failed refresh falls back to the existing
    (stale) cache rather than aborting the run; only a missing cache that also
    can't be fetched is fatal.
    """
    out_path = paths.nse_equity_list_raw()
    if _equity_list_cache_fresh(out_path, max_age_days):
        data = out_path.read_bytes()
    else:
        log.info("bhavcopy.equity_list.fetch", stale=out_path.exists())
        try:
            data = _fetch_with_retry(_EQUITY_LIST_URL)
        except TransientHttpError as e:
            data = None
            log.warning("bhavcopy.equity_list.refresh_failed", err=str(e))
        if data is not None:
            atomic_write_bytes(out_path, data)
        elif out_path.exists():
            log.warning("bhavcopy.equity_list.using_stale_cache", path=str(out_path))
            data = out_path.read_bytes()
        else:
            raise IngestError(f"bhavcopy: NSE EQUITY_L.csv unavailable (404): {_EQUITY_LIST_URL}")
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
        # Atomic write: the cache-hit check above is bare `cached.exists()`, so a
        # truncated partial download must never land at the final path.
        atomic_write_bytes(cached, data)

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
            continue  # unknown symbol — skip; recovered on the weekly EQUITY_L refresh
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


def ingest_recent(n_days: int = 75, *, full: bool = False) -> dict:
    """Walk the most recent n_days calendar days, ingesting each trading day.

    Default 75 days covers the 60-day median window with slack for holidays.
    Already-cached files are reused.

    Incremental by default: if stock_adv_daily already has data, only the days
    after its latest date (plus a small re-check overlap) are walked, rather than
    re-parsing all 75 cached files every run. The full history is preserved in
    the DB, so the trailing ADV window stays intact. ``full=True`` (or a cold,
    empty table) walks the entire n_days window. The day loop is idempotent
    (upsert), so the overlap is safe.

    Raises IngestError (never raw httpx errors) when NSE refuses a fetch —
    e.g. the archives endpoint's known intermittent 403 — so the pipeline
    halts via the clean failure path instead of a traceback.
    """
    today = date.today()
    if not full:
        latest = q.latest_stock_adv_date()
        if latest is not None:
            # +3-day overlap re-checks the most recent days (cheap, idempotent).
            n_days = min(n_days, (today - latest).days + 3)
            log.info("bhavcopy.recent.incremental", latest=latest.isoformat(), n_days=n_days)
    fetched = 0
    rows = 0
    try:
        symbol_to_isin = fetch_equity_list()
        for offset in range(1, n_days + 1):
            d = today - timedelta(days=offset)
            # Skip weekends (NSE is closed Sat/Sun).
            if d.weekday() >= 5:
                continue
            n = fetch_one(d, symbol_to_isin=symbol_to_isin)
            rows += n
            if n > 0:
                fetched += 1
    except (httpx.HTTPError, TransientHttpError) as e:
        # 403 from the archives endpoint is the proven case (_fetch_with_retry
        # raise_for_status); translate so _stage halts cleanly, not a traceback.
        raise IngestError(f"bhavcopy: NSE fetch failed: {e}") from e
    log.info("bhavcopy.recent.done", days_with_data=fetched, total_rows=rows)
    return {"days_with_data": fetched, "total_rows": rows}
