"""NSE Indices TRI ingestion (niftyindices.com).

The site's historical TRI section makes this call when you click "Get Data":

    POST https://www.niftyindices.com/Backpage.aspx/getTotalReturnIndexString
    Content-Type: application/json; charset=UTF-8
    {"cinfo": "{'name':'<TRADING_NAME_UPPER>','startDate':'<dd-Mon-yyyy>','endDate':'<dd-Mon-yyyy>','indexName':'<long_name>'}"}

The `cinfo` field is a string of single-quoted JSON (ASP.NET legacy). The endpoint
enforces a **365-day window cap** per request, so we chunk by year. Response shape:

    {"d": "[{\"Date\":\"15 Jan 2024\",\"TotalReturnsIndex\":\"32471.80\",...}, ...]"}

The Trading_Index_Name vs Index_long_name distinction comes from:
    https://iislliveblob.niftyindices.com/assets/json/IndexMapping.json

We deliberately avoid Yahoo Finance: Yahoo carries Price Return, not TRI, which
understates Indian fund alpha by ~1.5% annualized.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import httpx
import polars as pl
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from mfs import paths
from mfs.config import get_pipeline_config, get_settings
from mfs.db import queries as q
from mfs.db import writers as w
from mfs.errors import IngestError
from mfs.io.http import TransientHttpError
from mfs.utils.logging import get_logger

log = get_logger(__name__)

NIFTY_TRI_URL = "https://www.niftyindices.com/Backpage.aspx/getTotalReturnIndexString"
INDEX_MAPPING_URL = "https://iislliveblob.niftyindices.com/assets/json/IndexMapping.json"
TRI_WINDOW_DAYS = 360  # endpoint caps at 365; leave headroom

# Our canonical ticker -> (Trading_Index_Name uppercased, Index_long_name) as they appear
# in NSE's IndexMapping.json. These are hard-coded so a working pipeline doesn't depend on
# IndexMapping.json being reachable (its blob CDN is intermittently unavailable).
# Refresh by re-running `python scripts/refresh_index_mapping.py` if NSE renames anything.
NSE_TRI_MAP: dict[str, tuple[str, str]] = {
    "NIFTY 50 TRI": ("NIFTY 50", "Nifty 50"),
    "NIFTY 100 TRI": ("NIFTY 100", "Nifty 100"),
    "NIFTY 500 TRI": ("NIFTY 500", "Nifty 500"),
    "NIFTY LargeMidcap 250 TRI": ("NIFTY LARGEMID250", "NIFTY LargeMidcap 250"),
    "NIFTY Midcap 150 TRI": ("NIFTY MIDCAP 150", "Nifty Midcap 150"),
    "NIFTY Smallcap 250 TRI": ("NIFTY SMLCAP 250", "NIFTY Smallcap 250"),
    "NIFTY 500 Multicap 50:25:25 TRI": ("NIFTY500 MULTICAP", "NIFTY500 MULTICAP 50:25:25"),
    "NIFTY 500 Value 50 TRI": ("NIFTY500 VALUE 50", "Nifty500 Value 50"),
    "NIFTY Dividend Opportunities 50 TRI": ("NIFTY DIV OPPS 50", "Nifty Dividend Opportunities 50"),
    # Sector / thematic equity TRIs. (Trading_Index_Name, Index_long_name) sourced
    # from NSE's IndexMapping.json.
    "NIFTY Bank TRI":                ("NIFTY BANK", "Nifty Bank"),
    "NIFTY Financial Services TRI":  ("NIFTY FIN SERVICE", "Nifty Financial Services"),
    "NIFTY IT TRI":                  ("NIFTY IT", "Nifty IT"),
    "NIFTY Pharma TRI":              ("NIFTY PHARMA", "Nifty Pharma"),
    "NIFTY Healthcare TRI":          ("NIFTY HEALTHCARE", "Nifty Healthcare INDEX"),
    "NIFTY FMCG TRI":                ("NIFTY FMCG", "Nifty FMCG"),
    "NIFTY Auto TRI":                ("NIFTY AUTO", "Nifty Auto"),
    "NIFTY Energy TRI":              ("NIFTY ENERGY", "Nifty Energy"),
    # D5 (2026-06-13): candidate benchmark for the Energy category — ingested
    # so tools/benchmark_fit.py can evaluate it against the adopted NIFTY
    # Infrastructure TRI mapping (refit could not score it pre-ingest).
    # Names follow IndexMapping.json conventions; verify on first ingest via
    # _resolve_names('Nifty Commodities') if the fetch returns 0 rows.
    "NIFTY Commodities TRI":         ("NIFTY COMMODITIES", "Nifty Commodities"),
    "NIFTY Infrastructure TRI":      ("NIFTY INFRA", "Nifty Infrastructure"),
    "NIFTY PSE TRI":                 ("NIFTY PSE", "Nifty PSE"),
    "NIFTY India Consumption TRI":   ("NIFTY CONSUMPTION", "Nifty India Consumption"),
    "NIFTY MNC TRI":                 ("NIFTY MNC", "Nifty MNC"),
    "NIFTY100 ESG TRI":              ("NIFTY100 ESG", "NIFTY100 ESG"),
    "NIFTY India Manufacturing TRI": ("NIFTY INDIA MFG", "Nifty India Manufacturing"),
    # Hybrid TRIs (Nifty 50 Hybrid 65:35 / 50:50, Equity Savings) are NOT fetched
    # from NSE — niftyindices.com doesn't expose them via the public TRI endpoint.
    # They are synthesized from Nifty 50 TRI + risk-free in `synthetic_hybrid.py`,
    # invoked by the CLI after the equity ingest.
}

# Back-compat for callers that still reference the old constant name.
NSE_INDEX_NAME_MAP = {k: v[1] for k, v in NSE_TRI_MAP.items()}
NSE_LONG_NAME_MAP = NSE_INDEX_NAME_MAP


@lru_cache(maxsize=1)
def _load_index_mapping() -> list[dict]:
    """Fetch NSE's IndexMapping.json once and cache it for the process lifetime."""
    s = get_settings()
    with httpx.Client(timeout=s.http_timeout, headers={"User-Agent": s.user_agent}) as c:
        r = c.get(INDEX_MAPPING_URL)
        r.raise_for_status()
        # File is served as UTF-8 with BOM
        return json.loads(r.content.decode("utf-8-sig"))


def _resolve_names(long_name: str) -> tuple[str, str] | None:
    """Map our display long-name to (Trading_Index_Name uppercased, Index_long_name).

    Match is done by uppercasing + stripping whitespace on either Trading_Index_Name
    or Index_long_name — same logic NSE's own IndexMapping() function uses.
    """
    if not long_name:
        return None
    needle = re.sub(r"\s+", "", long_name).upper()
    try:
        mapping = _load_index_mapping()
    except Exception as e:  # noqa: BLE001
        log.warning("nse.index_mapping_fetch_failed", err=str(e))
        return None
    for row in mapping:
        long_ = row.get("Index_long_name", "")
        trad = row.get("Trading_Index_Name", "")
        if re.sub(r"\s+", "", long_).upper() == needle or re.sub(r"\s+", "", trad).upper() == needle:
            return trad.upper().strip(), long_
    return None


def _fmt_amfi_style(d: date) -> str:
    """Format date as `dd-Mon-yyyy` (e.g., 01-Jan-2024) per the site's datepicker."""
    return d.strftime("%d-%b-%Y")


def ticker_slug(ticker: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", ticker).strip("_").lower()


def _fmt(d: date) -> str:
    return d.strftime("%d-%b-%Y")


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type(TransientHttpError),
)
def fetch_tri_window(trading_name_upper: str, long_name: str, start: date, end: date) -> list[dict]:
    """Fetch one TRI window from niftyindices.com. Window must be ≤ 360 days.

    Returns the parsed inner array of {Date, TotalReturnsIndex, ...} dicts.
    """
    s = get_settings()
    # Inner payload uses single quotes — ASP.NET method-page legacy
    inner = (
        "{"
        f"'name':'{trading_name_upper}',"
        f"'startDate':'{_fmt_amfi_style(start)}',"
        f"'endDate':'{_fmt_amfi_style(end)}',"
        f"'indexName':'{long_name}'"
        "}"
    )
    body = json.dumps({"cinfo": inner})
    headers = {
        "User-Agent": s.user_agent,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": "https://www.niftyindices.com/reports/historical-data",
        "Origin": "https://www.niftyindices.com",
        "X-Requested-With": "XMLHttpRequest",
    }
    # C3: explicit per-phase timeouts instead of the blanket 60s — a hung NSE
    # read fails into the tenacity retry in ≤30s, bounding the per-window
    # worst case that produced the audit's 33.8-min benchmark stage.
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0)
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as c:
        try:
            r = c.post(NIFTY_TRI_URL, content=body)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            raise TransientHttpError(str(e)) from e
        if r.status_code in (429, 500, 502, 503, 504):
            raise TransientHttpError(f"{r.status_code} from NSE Indices")
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # 4xx is not transient — translate to IngestError with source
            # context so it surfaces cleanly (per-ticker isolation in
            # ingest_all_known aggregates these into the stage failure).
            raise IngestError(
                f"NSE Indices {r.status_code} for {long_name!r} at {NIFTY_TRI_URL}"
            ) from e
        outer = r.json()
    if not isinstance(outer, dict) or "d" not in outer:
        raise TransientHttpError(f"Unexpected NSE envelope: {type(outer).__name__}")
    inner_str = outer["d"]
    if isinstance(inner_str, list):
        return inner_str
    if not isinstance(inner_str, str):
        raise TransientHttpError(f"Unexpected NSE 'd' shape: {type(inner_str).__name__}")
    try:
        return json.loads(inner_str)
    except json.JSONDecodeError as e:
        raise TransientHttpError(f"Bad inner JSON: {e}") from e


def fetch_tri_series(trading_name_upper: str, long_name: str, start: date, end: date) -> list[dict]:
    """Fetch TRI series for [start, end] in 360-day chunks. Returns deduped, sorted records."""
    all_records: list[dict] = []
    cur = start
    while cur <= end:
        nxt = min(end, cur + timedelta(days=TRI_WINDOW_DAYS - 1))
        recs = fetch_tri_window(trading_name_upper, long_name, cur, nxt)
        all_records.extend(recs)
        log.info(
            "nse.tri.window",
            index=long_name,
            from_=cur.isoformat(),
            to=nxt.isoformat(),
            rows=len(recs),
        )
        cur = nxt + timedelta(days=1)
    return all_records


def _parse_records(records: list[dict]) -> pl.DataFrame:
    """Parse NSE TRI records. Observed shape:
        { "Index Name": "...", "Date": "15 Jan 2024", "TotalReturnsIndex": "32471.80", "NTR_Value": "..." }
    """
    if not records:
        return pl.DataFrame()
    norm = []
    for rec in records:
        d_str = (
            rec.get("Date")
            or rec.get("HistoricalDate")
            or rec.get("TradeDate")
        )
        v_str = (
            rec.get("TotalReturnsIndex")
            or rec.get("TR")
            or rec.get("Close")
            or rec.get("IndexValue")
            or rec.get("TotalReturnIndex")
        )
        if d_str is None or v_str is None:
            continue
        d = None
        for fmt in ("%d %b %Y", "%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                d = datetime.strptime(str(d_str).strip(), fmt).date()
                break
            except ValueError:
                continue
        if d is None:
            continue
        try:
            v = float(str(v_str).replace(",", ""))
        except ValueError:
            continue
        norm.append({"date": d, "close": v})
    if not norm:
        return pl.DataFrame()
    return pl.DataFrame(norm).with_columns(pl.col("date").cast(pl.Date)).unique("date").sort("date")


def _save_raw(records: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(records))


_DATE_COL_CANDIDATES = ("date", "index date", "trade date", "historical date")
_VALUE_COL_CANDIDATES = (
    "close",
    "total returns index",
    "totalreturnsindex",
    "total return index",
    "tri",
    "tr",
    "index value",
    "indexvalue",
)
_DATE_FORMATS = ("%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y")


def _parse_date_expr(col_name: str) -> pl.Expr:
    """Build an Expr that tries multiple date formats and coalesces the first that parses."""
    base = pl.col(col_name).cast(pl.Utf8)
    return pl.coalesce(
        *[base.str.strptime(pl.Date, format=fmt, strict=False) for fmt in _DATE_FORMATS]
    )


def _ingest_from_manual_csv(ticker: str) -> pl.DataFrame:
    """Read a user-dropped CSV at data/raw/benchmarks/manual/<slug>.csv.

    Tolerant of niftyindices.com's native column names:
      - date column may be "Date", "Index Date", etc.
      - value column may be "Close" (price), "Total Returns Index" (TRI), etc.
    Returns columns: date, close.
    """
    slug = ticker_slug(ticker)
    candidate = paths.raw_dir() / "benchmarks" / "manual" / f"{slug}.csv"
    if not candidate.exists():
        return pl.DataFrame()
    try:
        df = pl.read_csv(candidate, infer_schema_length=1000, try_parse_dates=False)
    except Exception as e:  # noqa: BLE001
        log.warning("benchmark.manual.read_failed", file=str(candidate), err=str(e))
        return pl.DataFrame()

    lower_map = {c: c.strip().lower() for c in df.columns}
    df = df.rename(lower_map)

    date_col = next((c for c in _DATE_COL_CANDIDATES if c in df.columns), None)
    value_col = next((c for c in _VALUE_COL_CANDIDATES if c in df.columns), None)
    if not date_col or not value_col:
        log.warning(
            "benchmark.manual.missing_cols",
            file=str(candidate),
            have=list(df.columns),
            need_date_one_of=_DATE_COL_CANDIDATES,
            need_value_one_of=_VALUE_COL_CANDIDATES,
        )
        return pl.DataFrame()

    # Clean numeric strings (niftyindices.com sometimes uses commas as thousands separators)
    value_clean = (
        pl.col(value_col).cast(pl.Utf8).str.replace_all(",", "").cast(pl.Float64, strict=False)
    )

    out = (
        df.select(
            _parse_date_expr(date_col).alias("date"),
            value_clean.alias("close"),
        )
        .drop_nulls()
        .unique("date")
        .sort("date")
    )
    log.info(
        "benchmark.manual.loaded",
        ticker=ticker,
        rows=out.height,
        date_col=date_col,
        value_col=value_col,
    )
    return out


def _resolve_start(
    ticker: str, cfg, *, full: bool, latest_map: dict[str, date] | None
) -> date:
    """Compute the per-ticker fetch start for an incremental run.

    - ``full=True`` → full history (the periodic deep re-fetch / ``--full``).
    - ticker has no rows yet → full history (cold start; can't tail from nothing).
    - otherwise → ``MAX(date) - incremental_tail_days`` so only the recent window
      is re-fetched. The tail overlaps existing data; the upsert overwrites it,
      so any NSE restatement inside the tail self-corrects. Data older than the
      tail is assumed immutable (re-fetch it with ``--full`` if ever needed).
    """
    history_start = cfg.ingest.benchmarks.history_start
    if full:
        return history_start
    latest = (
        latest_map.get(ticker) if latest_map is not None
        else q.benchmark_latest_by_ticker().get(ticker)
    )
    if latest is None:
        return history_start
    tail = cfg.ingest.benchmarks.incremental_tail_days
    return max(history_start, latest - timedelta(days=tail))


def ingest_ticker(
    ticker: str, start: date | None = None, end: date | None = None,
    *, full: bool = False,
) -> int:
    """Fetch one TRI ticker for [start, end] and merge into the curated dataset.

    Uses the hard-coded NSE_TRI_MAP for (Trading_Index_Name, Index_long_name); fetches in
    360-day chunks; falls back to user CSV at data/raw/benchmarks/manual/<slug>.csv when
    the API returns empty or the ticker has no NSE mapping (hybrid indices).

    When ``start`` is not given, it is resolved incrementally from the DB
    (``MAX(date) - tail``); pass ``full=True`` to force the full history.
    """
    cfg = get_pipeline_config()
    start = start or _resolve_start(ticker, cfg, full=full, latest_map=None)
    end = end or date.today()
    slug = ticker_slug(ticker)
    df: pl.DataFrame = pl.DataFrame()

    trading_upper, long_name_canonical = NSE_TRI_MAP.get(ticker, ("", ""))
    if trading_upper and long_name_canonical:
        try:
            records = fetch_tri_series(trading_upper, long_name_canonical, start, end)
            raw_path = paths.benchmark_raw(
                slug, f"{start.isoformat()}_{end.isoformat()}"
            ).with_suffix(".json")
            _save_raw(records, raw_path)
            df = _parse_records(records)
        except Exception as e:  # noqa: BLE001
            log.warning("benchmark.api_failed", ticker=ticker, err=str(e))
    else:
        log.info("benchmark.no_nse_mapping", ticker=ticker, hint="use manual CSV fallback")

    if df.is_empty():
        df = _ingest_from_manual_csv(ticker)

    if df.is_empty():
        log.warning(
            "benchmark.empty",
            ticker=ticker,
            hint="drop a CSV at data/raw/benchmarks/manual/<slug>.csv with columns date,close",
        )
        return 0
    df = df.with_columns(pl.lit(ticker).alias("ticker"))
    w.upsert_benchmark_daily(df.select(["ticker", "date", "close"]))
    log.info("benchmark.ingested", ticker=ticker, rows=df.height)
    return df.height


def ingest_all_known(
    start: date | None = None, end: date | None = None, *, full: bool = False,
) -> dict[str, int]:
    """Ingest every TRI ticker we have a mapping for. Returns {ticker: n_rows}.

    Incremental by default: each ticker is fetched from ``MAX(date) - tail``
    forward (full history for tickers with no rows). Pass ``full=True`` for a
    from-scratch re-fetch of the entire history. An explicit ``start`` overrides
    incremental resolution for every ticker (e.g. the CLI ``--since`` flag).

    Raises IngestError if any EQUITY TRI ticker (i.e., one with a non-empty NSE
    mapping in NSE_TRI_MAP) returns 0 rows after retries. Hybrid/multi-asset
    tickers with empty mappings are exempt — by design they require manual CSVs
    and are reported as empty with the `benchmark.empty` warning only.

    Concurrency (C3): tickers run on a ThreadPool of
    ``settings.benchmark_workers`` (env ``MFS_BENCHMARK_WORKERS``, default 3 —
    every ticker hits the same niftyindices.com host, so the pool stays small
    to bound NSE load; 1 forces serial). tenacity already retries 429/5xx per
    window. The aggregate IngestError is raised only after ALL tickers finish
    (same message format, every failed equity ticker listed); per-ticker DB
    upserts are thread-safe (fresh per-call connections, disjoint ticker PK
    ranges). Aggregation is deterministic in NSE_INDEX_NAME_MAP order.
    """
    out: dict[str, int] = {}
    equity_failures: list[str] = []
    # Prefetch per-ticker watermarks once, BEFORE the pool starts, so each
    # ticker doesn't re-query.
    latest_map = None if start is not None else q.benchmark_latest_by_ticker()
    cfg = get_pipeline_config()
    tickers = list(NSE_INDEX_NAME_MAP)
    if not tickers:
        return out

    def _one(ticker: str) -> int:
        t_start = start or _resolve_start(ticker, cfg, full=full, latest_map=latest_map)
        return ingest_ticker(ticker, start=t_start, end=end)

    n_workers = min(max(1, int(get_settings().benchmark_workers)), len(tickers))
    outcomes: dict[str, int | Exception] = {}
    if n_workers == 1:
        for ticker in tickers:
            try:
                outcomes[ticker] = _one(ticker)
            except Exception as e:  # noqa: BLE001 — per-ticker isolation; aggregate below
                outcomes[ticker] = e
    else:
        with ThreadPoolExecutor(
            max_workers=n_workers, thread_name_prefix="bench-ingest"
        ) as pool:
            futures = {t: pool.submit(_one, t) for t in tickers}
            for ticker, fut in futures.items():
                try:
                    outcomes[ticker] = fut.result()
                except Exception as e:  # noqa: BLE001 — per-ticker isolation; aggregate below
                    outcomes[ticker] = e

    for ticker in tickers:
        has_nse_mapping = bool(NSE_TRI_MAP.get(ticker, ("", ""))[0])
        o = outcomes[ticker]
        if isinstance(o, Exception):
            log.error("benchmark.failed", ticker=ticker, err=str(o))
            out[ticker] = 0
            if has_nse_mapping:
                equity_failures.append(f"{ticker}: {o}")
            continue
        out[ticker] = o
        if o == 0 and has_nse_mapping:
            equity_failures.append(f"{ticker}: ingest returned 0 rows")
    if equity_failures:
        raise IngestError(
            f"NSE equity TRI ingest: {len(equity_failures)} ticker(s) failed:\n  - "
            + "\n  - ".join(equity_failures)
        )
    return out
