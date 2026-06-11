"""91-day T-bill yield ingestion (risk-free rate).

Data sources tried in order:

  1. Manual CSVs at data/raw/fbil_tbill/manual/*.csv with columns
     `date, rate_annual_pct` (date in YYYY-MM-DD). Highest priority — lets the
     user override or supply pre-RBI-press-release historical data.
  2. RBI Press Releases (HTML scrape of BS_PressReleaseDisplay.aspx?prid=N).
     Each weekly "91-Day, 182-Day and 364-Day T-Bill Auction Result: Cut-off"
     release contains the 91-day implicit YTM in the HTML table.
  3. Optional fallback constant — opt-in via allow_fallback=True only. Default
     behavior raises rather than silently writing synthetic data.

The curated output is a single Parquet at data/curated/risk_free_daily/risk_free.parquet
with columns (date, rate_annual, rate_daily) where rate_annual is decimal (0.07 = 7%) and
rate_daily = (1 + rate_annual)^(1/252) - 1.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from mfs import paths
from mfs.config import get_pipeline_config, get_settings
from mfs.db import writers as w
from mfs.errors import IngestError
from mfs.io.atomic import atomic_write_text
from mfs.io.http import TransientHttpError
from mfs.utils.logging import get_logger

log = get_logger(__name__)

FALLBACK_RATE_ANNUAL = 0.065  # 6.5% — only used when allow_fallback=True

RBI_PRESS_URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"
RBI_RSS_URL = "https://www.rbi.org.in/pressreleases_RSS.xml"

# Approximate prid issue rate at RBI (~2500/year). Used for bounding the backwards walk.
PRIDS_PER_YEAR = 2600

# Probe-forward discovery (see discover_latest_prid). RBI prids are dense
# (~7/day), so this many consecutive absent prids reliably marks the feed tip.
PROBE_FORWARD_GAP = 12
# Hard cap on how far past the anchor we probe in one run (~8 weeks of prids) so
# a pathological feed can never loop unbounded.
PROBE_FORWARD_MAX = 400

# Title patterns for RBI T-bill auction press releases. Two PR variants both
# contain the 91-day cut-off yield:
#   (a) Cut-off PR: title like "91 days, 182 days and 364 days T-Bill Auction
#       Result: Cut off" (older) or "91-Day, 182-Day and 364-Day T-Bill Auction
#       Result: Cut-off" (newer).
#   (b) Full Auction Result PR: title "Treasury Bills: Full Auction Result"
#       published alongside (a) on the same auction date.
# Some weeks RBI publishes only (b). The parser accepts either.
_TENORS_RE = re.compile(
    r"91\s*-?\s*[Dd]ays?.{1,15}182\s*-?\s*[Dd]ays?.{1,25}364\s*-?\s*[Dd]ays?",
    re.IGNORECASE,
)
_TBILL_RESULT_TITLE_RE = re.compile(
    r"(?:T-?Bill|Treasury\s+Bills?)\s*:?\s*(?:Full\s+)?Auction\s+Result",
    re.IGNORECASE,
)
_FULL_AUCTION_RESULT_RE = re.compile(r"Full\s+Auction\s+Result", re.IGNORECASE)
# Old RBI PRs (pre-2016) publish ONE auction-result PR per tenor — e.g.
# "91-Days Treasury Bills : Full Auction Result", "182-Days ...", "364-Days ...",
# and occasionally odd-tenor / MSS bills ("329-Days", "317-Days MSS-..."). We
# must accept only the 91-day one; otherwise another tenor's YTM is mislabeled as
# the 91-day rate. New PRs combine all tenors in a single table (91-day row
# first), titled either "91-Day, 182-Day and 364-Day ..." or the tenor-less
# "Treasury Bills: Full Auction Result".
_TENOR_ANY_RE = re.compile(r"(\d+)\s*-?\s*[Dd]ays?")
# Title block lives inside <td class="tableheader" ...>; the <b> wrapper is
# present on the Cut-off variant but absent on the Full Auction Result variant,
# so we accept both forms.
_TITLE_BLOCK_RE = re.compile(
    r'class="tableheader"[^>]*>(?:\s*<b>)?\s*([^<]{1,300})',
    re.IGNORECASE,
)


def _tbill_result_title(html: str) -> str | None:
    """Return the auction-result title block if this page is one, else None."""
    for t in _TITLE_BLOCK_RE.findall(html):
        if _TBILL_RESULT_TITLE_RE.search(t) and (
            _TENORS_RE.search(t) or _FULL_AUCTION_RESULT_RE.search(t)
        ):
            return t
    return None


def _is_91day_result(title: str) -> bool:
    """True if this auction-result PR carries the 91-day cut-off.

    If the title names any tenor(s), accept only when 91 is among them (covers
    the old per-tenor "91-Days ..." PRs and the new combined "91-Day, 182-Day and
    364-Day ..." cut-off PRs, while rejecting 182/364/odd-tenor/MSS PRs). A
    tenor-less title ("Treasury Bills: Full Auction Result") is the combined
    full-auction table whose first cut-off row is the 91-day, so accept it.
    """
    days = _TENOR_ANY_RE.findall(title)
    if days:
        return "91" in days
    return True


def _looks_like_tbill_result(html: str) -> bool:
    """True if the page is a 91/182/364-day T-bill auction result PR (either variant)."""
    return _tbill_result_title(html) is not None
_DATE_RE = re.compile(
    r"Date\s*:\s*(\w+\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
# Unified YTM extractor covering both PR formats. Anchor on a "Cut-off" token
# followed by "Price" or "Yield" within a short window (excluding tag chars),
# then take the FIRST YTM percentage after it — that's the 91-day cell.
#   Cut-off variant: "Cut-off Price and Implicit Yield ... YTM: 3.5594%"
#   Full Auction Result variant: "Cut-off price / Yield ... (YTM: 3.5594%)"
# The separator between "YTM" and the value varies by era: new PRs use a colon
# ("YTM : 3.5594%"), old PRs a hyphen ("YTM - 8.1022%"). Require a decimal so the
# degenerate no-issuance case ("YTM : 0 %") stays correctly unmatched.
_YTM_BLOCK_RE = re.compile(
    r"Cut-?off[^<>]{0,80}(?:Price|Yield).*?YTM\s*[:\-–]?\s*([0-9]+\.[0-9]+)",
    re.IGNORECASE | re.DOTALL,
)


def _build_daily_series(rate_by_date: dict[date, float], start: date, end: date) -> pl.DataFrame:
    """Forward-fill a sparse rate series across [start, end] daily; convert to decimal + daily rate."""
    cfg = get_pipeline_config()
    n_days = cfg.returns.trading_days_per_year
    days: list[date] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    last_rate: float | None = None
    rows = []
    for d in days:
        if d in rate_by_date:
            last_rate = rate_by_date[d]
        if last_rate is None:
            continue
        ann = last_rate / 100.0 if last_rate > 1.5 else last_rate  # accept pct or decimal input
        daily = (1.0 + ann) ** (1.0 / n_days) - 1.0
        rows.append({"date": d, "rate_annual": ann, "rate_daily": daily})
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


# ---------------------------------------------------------------------------
# Path 1: manual CSVs
# ---------------------------------------------------------------------------


def _load_manual_csvs() -> dict[date, float]:
    """Return {date: rate_annual_pct} from any user-provided CSVs (empty dict if none)."""
    manual_dir = paths.raw_dir() / "fbil_tbill" / "manual"
    if not manual_dir.exists():
        return {}
    out: dict[date, float] = {}
    for csv in sorted(manual_dir.glob("*.csv")):
        try:
            df = pl.read_csv(csv)
        except Exception as e:  # noqa: BLE001
            log.warning("fbil.manual.parse_failed", file=str(csv), err=str(e))
            continue
        df.columns = [c.strip().lower() for c in df.columns]
        if "date" not in df.columns:
            continue
        rate_col = next((c for c in df.columns if "rate" in c or "yield" in c), None)
        if not rate_col:
            continue
        df = df.select(
            pl.col("date").str.to_date(strict=False).alias("date"),
            pl.col(rate_col).cast(pl.Float64).alias("rate_annual_pct"),
        ).drop_nulls()
        for r in df.iter_rows(named=True):
            out[r["date"]] = r["rate_annual_pct"]
    return out


# ---------------------------------------------------------------------------
# Path 2: RBI press-release scraper
# ---------------------------------------------------------------------------


def _rbi_client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(
        timeout=s.http_timeout,
        headers={"User-Agent": s.user_agent},
        follow_redirects=True,
    )


def _parse_month_day_year(text: str) -> date | None:
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _read_state() -> dict:
    p = paths.rbi_tbill_state_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _write_state(state: dict) -> None:
    # Atomic write: a kill mid-write must not truncate the state JSON. (Readers
    # already fall back to {}, but an atomic write removes the corruption window.)
    p = paths.rbi_tbill_state_file()
    atomic_write_text(p, json.dumps(state, indent=2, sort_keys=True))


def fetch_latest_prid(default: int | None = None) -> int | None:
    """Return the highest prid currently visible in the RBI press-release RSS feed."""
    try:
        with _rbi_client() as c:
            r = c.get(RBI_RSS_URL)
            r.raise_for_status()
        prids = [int(x) for x in re.findall(r"prid=(\d+)", r.text)]
        if prids:
            latest = max(prids)
            log.info("fbil.rbi.rss_latest_prid", prid=latest)
            return latest
    except Exception as e:  # noqa: BLE001
        log.warning("fbil.rbi.rss_failed", err=str(e))
    return default


class _PermanentFetchError(Exception):
    """The prid does not exist or returned a non-recoverable response — do not retry."""


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(TransientHttpError),
)
def _fetch_prid_with_retry(client: httpx.Client, prid: int) -> str:
    """Single HTTP call with retry on transient (5xx / network) errors only.

    Raises TransientHttpError after retries exhausted (caller treats as a hard
    fetch failure). Raises _PermanentFetchError for 404 / empty bodies (caller
    treats as "prid does not exist", not a failure).
    """
    try:
        r = client.get(RBI_PRESS_URL, params={"prid": prid})
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
        raise TransientHttpError(str(e)) from e
    if r.status_code in (429, 500, 502, 503, 504):
        raise TransientHttpError(f"{r.status_code} from RBI for prid={prid}")
    if r.status_code == 404:
        raise _PermanentFetchError(f"prid {prid}: 404 not found")
    if r.status_code != 200:
        raise TransientHttpError(f"unexpected status {r.status_code} for prid={prid}")
    # Status 200 but tiny body = RBI rate-limit truncation. We previously treated
    # this as permanent and skipped forever; that lost ~91% of PRs in some
    # windows and is the root cause of the historical gaps. Treat as transient.
    if not r.text or len(r.text) < 500:
        raise TransientHttpError(
            f"prid {prid}: 200 OK but body_len={len(r.text)} (likely rate-limited)"
        )
    return r.text


def _fetch_prid_html(client: httpx.Client, prid: int) -> tuple[str | None, str | None]:
    """Fetch one prid page; cache to disk.

    Returns (html_or_None, failure_reason_or_None):
      - (html, None)  → success
      - (None, None)  → permanent absence (404 / empty body) — not a failure
      - (None, "..."  → transient failure after retries — counted toward the cap
    """
    cache = paths.rbi_press_release_raw(prid)
    if cache.exists() and cache.stat().st_size > 200:
        return cache.read_text(encoding="utf-8", errors="ignore"), None
    try:
        text = _fetch_prid_with_retry(client, prid)
    except _PermanentFetchError:
        return None, None
    except TransientHttpError as e:
        log.warning("fbil.rbi.fetch_failed_after_retries", prid=prid, err=str(e))
        return None, str(e)
    # Atomic write so a kill mid-download can't strand a truncated page that the
    # `>200 bytes` cache check (at the top of this fn and in _walk_prid_range)
    # would otherwise trust as a valid PR and never re-fetch.
    atomic_write_text(cache, text)
    return text, None


def _cache_high_water_mark() -> int:
    """Highest prid already cached on disk (0 if none). A hard floor for
    discovery — a stale RSS must never drag the walk below what we already have."""
    cache_dir = paths.rbi_press_release_raw(0).parent
    if not cache_dir.exists():
        return 0
    hwm = 0
    for f in cache_dir.glob("prid_*.html"):
        try:
            hwm = max(hwm, int(f.stem.replace("prid_", "")))
        except ValueError:
            continue
    return hwm


def discover_latest_prid(
    state: dict | None = None, *, rate_limit_s: float = 0.25
) -> int | None:
    """Resolve the true upper-bound prid for the walk.

    The RSS feed is a HINT, never a ceiling: it has been observed to serve a
    stale/truncated snapshot whose max prid is *below* prids we already cached
    weeks earlier, which silently capped discovery and stranded newer auctions
    (the bug that motivated this). We anchor at

        max(RSS latest, last-walked state.latest_prid, on-disk cache HWM)

    then probe forward one prid at a time until PROBE_FORWARD_GAP consecutive
    prids are confirmed-absent (404 / empty body) — that run of holes is the real
    tip. A *transient* error mid-probe is NOT read as the tip (a rate-limit burst
    must never look like end-of-feed); we stop probing and let the walk's
    failure-rate cap and the freshness gate judge the degraded feed instead.

    Returns the highest real prid found, or None if nothing is known at all.
    """
    state = state if state is not None else _read_state()
    rss = fetch_latest_prid() or 0
    state_latest = state.get("latest_prid") or 0
    cache_hwm = _cache_high_water_mark()
    anchor = max(rss, state_latest, cache_hwm)
    if anchor <= 0:
        return None
    if rss and rss < max(state_latest, cache_hwm):
        log.warning(
            "fbil.rbi.rss_behind_known",
            rss=rss, state_latest=state_latest, cache_hwm=cache_hwm,
            note="RSS reported a prid below known state/cache — probing forward, not trusting it.",
        )
    tip = anchor
    consecutive_absent = 0
    probed = 0
    with _rbi_client() as c:
        prid = anchor + 1
        while consecutive_absent < PROBE_FORWARD_GAP and probed < PROBE_FORWARD_MAX:
            html, failure = _fetch_prid_html(c, prid)
            if failure is not None:
                # Transient exhaustion — do NOT treat as the tip; abort the probe.
                log.warning("fbil.rbi.probe_transient_abort", prid=prid, err=failure)
                break
            if html is None:
                consecutive_absent += 1
            else:
                consecutive_absent = 0
                tip = prid
            prid += 1
            probed += 1
            time.sleep(rate_limit_s)
    log.info(
        "fbil.rbi.discover_latest_prid",
        rss=rss, state_latest=state_latest, cache_hwm=cache_hwm,
        anchor=anchor, tip=tip, probed=probed,
    )
    return tip


def _parse_tbill_html(html: str, prid: int) -> dict | None:
    """If HTML is a 91-day T-bill auction-result PR (Cut-off OR Full Auction Result
    variant), return {prid, auction_date, rate_annual_pct}; otherwise None."""
    title = _tbill_result_title(html)
    if title is None or not _is_91day_result(title):
        return None
    m_date = _DATE_RE.search(html)
    m_ytm = _YTM_BLOCK_RE.search(html)
    if not (m_date and m_ytm):
        return None
    auction_date = _parse_month_day_year(m_date.group(1))
    if auction_date is None:
        return None
    try:
        rate_pct = float(m_ytm.group(1))
    except ValueError:
        return None
    return {"prid": prid, "auction_date": auction_date, "rate_annual_pct": rate_pct}


def _walk_prid_range(
    prid_lo: int,
    prid_hi: int,
    *,
    max_workers: int = 4,
    rate_limit_s: float = 0.25,
) -> tuple[list[dict], int]:
    """Fetch prids in [prid_lo, prid_hi] inclusive.

    Returns (hits, n_failed). A "failure" is a prid whose HTTP request exhausted
    its retry budget — not a prid that simply doesn't exist. Caller enforces the
    failure-rate cap by comparing n_failed / n_prids to the configured threshold.
    """
    prids = list(range(prid_lo, prid_hi + 1))
    log.info("fbil.rbi.walk.start", prid_lo=prid_lo, prid_hi=prid_hi, n=len(prids))
    results: list[dict] = []
    n_failed = 0
    completed = 0

    def _do(prid: int) -> tuple[dict | None, str | None]:
        # Cache hits skip the rate-limit sleep entirely so gap-fill walks over a
        # mostly-cached range stay fast.
        cache = paths.rbi_press_release_raw(prid)
        if cache.exists() and cache.stat().st_size > 200:
            try:
                html = cache.read_text(encoding="utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                html = None
            if html is not None:
                return _parse_tbill_html(html, prid), None
        time.sleep(rate_limit_s)
        with _rbi_client() as c:
            html, failure = _fetch_prid_html(c, prid)
        if html is None:
            return None, failure
        return _parse_tbill_html(html, prid), None

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_do, p): p for p in prids}
        for fut in as_completed(futures):
            completed += 1
            try:
                hit, failure = fut.result()
            except Exception as e:  # noqa: BLE001
                # Unexpected — bug in worker. Count as a failure to be safe.
                log.error("fbil.rbi.worker_crashed", prid=futures[fut], err=str(e))
                n_failed += 1
                continue
            if failure is not None:
                n_failed += 1
            if hit:
                results.append(hit)
            if completed % 500 == 0:
                log.info(
                    "fbil.rbi.walk.progress",
                    completed=completed,
                    total=len(prids),
                    hits=len(results),
                    failed=n_failed,
                )
    log.info(
        "fbil.rbi.walk.done",
        n_prids=len(prids),
        n_hits=len(results),
        n_failed=n_failed,
    )
    return results, n_failed


def _scraped_csv_path() -> Path:
    return paths.raw_dir() / "fbil_tbill" / "rbi_scraped_91d_tbill.csv"


def _load_scraped_csv() -> dict[date, float]:
    p = _scraped_csv_path()
    if not p.exists():
        return {}
    try:
        df = pl.read_csv(p, schema_overrides={"date": pl.Date})
    except Exception as e:  # noqa: BLE001
        log.warning("fbil.rbi.scraped_csv_parse_failed", err=str(e))
        return {}
    return {r["date"]: r["rate_annual_pct"] for r in df.iter_rows(named=True)}


def _save_scraped_csv(rates: dict[date, float]) -> None:
    p = _scraped_csv_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    df = (
        pl.DataFrame(
            [{"date": d, "rate_annual_pct": r} for d, r in rates.items()],
            schema={"date": pl.Date, "rate_annual_pct": pl.Float64},
        )
        .sort("date")
    )
    # Atomic replace (write tmp sibling, then rename) so a kill mid-write can't
    # leave a truncated CSV.
    tmp = p.with_suffix(p.suffix + ".tmp")
    df.write_csv(tmp)
    tmp.replace(p)


def reparse_cache() -> dict[date, float]:
    """Re-parse every cached PR HTML and return {auction_date: rate_annual_pct}.

    Useful after parser improvements (e.g., adding a new title variant): the
    cache is the source of truth, so we recompute the date→rate map locally
    rather than re-fetching from RBI.
    """
    cache_dir = paths.rbi_press_release_raw(0).parent
    if not cache_dir.exists():
        return {}
    out: dict[date, float] = {}
    n_files = 0
    n_parsed = 0
    for f in cache_dir.glob("prid_*.html"):
        n_files += 1
        try:
            prid = int(f.stem.replace("prid_", ""))
        except ValueError:
            continue
        try:
            html = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        parsed = _parse_tbill_html(html, prid)
        if parsed:
            n_parsed += 1
            out[parsed["auction_date"]] = parsed["rate_annual_pct"]
    log.info(
        "fbil.rbi.reparse_cache",
        n_files=n_files,
        n_parsed=n_parsed,
        n_unique_dates=len(out),
    )
    return out


def ingest_from_rbi_press_releases(
    *,
    years: float = 13.5,
    start_prid: int | None = None,
    end_prid: int | None = None,
    max_workers: int = 4,
    gap_fill: bool = True,
) -> dict[date, float]:
    """Scrape RBI T-bill cut-off press releases; return {auction_date: rate_annual_pct}.

    Strategy: walk a contiguous prid range and pluck out the ~52/year T-bill
    auction-result releases. Caches every fetched HTML to disk, and persists a
    per-date CSV so repeated runs only fetch new prids beyond the last-seen
    high-water mark.

    `gap_fill=True` (default) re-attempts every prid in the previously-walked
    range that is missing from the cache. This is how we recover from the
    historical bug where rate-limited tiny-body responses got swallowed as
    permanent failures. Cached prids are not re-fetched (cache check is inside
    `_fetch_prid_html`), so the cost is roughly one HTTP call per missing prid.
    """
    state = _read_state()
    # Always rebuild the in-memory rate dict by re-parsing the on-disk cache,
    # so parser improvements (new title variants etc.) take effect even when no
    # new prids are walked.
    existing = reparse_cache() or _load_scraped_csv()

    # Determine end_prid (upper bound to walk). Use probe-forward discovery, not
    # the raw RSS max — a stale RSS must never cap the walk below the true tip.
    if end_prid is None:
        end_prid = discover_latest_prid(state)
        if end_prid is None:
            log.error("fbil.rbi.no_latest_prid")
            return existing

    # Determine start_prid (lower bound).
    if start_prid is None:
        prior_oldest = state.get("oldest_prid_walked")
        prior_latest = state.get("latest_prid")
        backfill_floor = max(1, int(end_prid - years * PRIDS_PER_YEAR))
        if prior_oldest is not None and prior_oldest <= backfill_floor:
            # We already have ≥ `years` of history. Incremental: walk from a
            # 50-prid tail re-check up to end_prid.
            start_prid = max(prior_oldest, (prior_latest or end_prid) - 50)
        else:
            # Need to extend history backwards (or no prior state).
            start_prid = backfill_floor

    # Optional gap-fill pass: re-walk the full historical range so any prids
    # that previously failed transiently (and got mis-classified as permanent)
    # are re-attempted. `_fetch_prid_html` skips already-cached prids, so this
    # only HTTPs the holes.
    if gap_fill and state.get("oldest_prid_walked") is not None:
        gap_lo = state["oldest_prid_walked"]
        gap_hi = end_prid
        log.info("fbil.rbi.gap_fill.start", prid_lo=gap_lo, prid_hi=gap_hi)
        gap_hits, gap_failed = _walk_prid_range(gap_lo, gap_hi, max_workers=max_workers)
        for h in gap_hits:
            existing[h["auction_date"]] = h["rate_annual_pct"]
        log.info(
            "fbil.rbi.gap_fill.done",
            n_new_hits=len(gap_hits),
            n_failed=gap_failed,
            n_total_dates=len(existing),
        )

    if start_prid > end_prid:
        log.info("fbil.rbi.already_current", start_prid=start_prid, end_prid=end_prid)
        _save_scraped_csv(existing)
        return existing

    hits, n_failed = _walk_prid_range(start_prid, end_prid, max_workers=max_workers)
    n_walked = end_prid - start_prid + 1
    failure_rate = n_failed / max(n_walked, 1)
    max_failure_rate = get_pipeline_config().freshness.max_tbill_scrape_failure_rate
    if failure_rate > max_failure_rate:
        raise IngestError(
            f"RBI T-bill walk: {n_failed}/{n_walked} prids failed after retries "
            f"({failure_rate:.1%} > max {max_failure_rate:.1%}). Network/RBI degraded — "
            f"do not proceed with partial risk-free data."
        )

    # Merge hits into the persistent scraped CSV.
    for h in hits:
        existing[h["auction_date"]] = h["rate_annual_pct"]

    _save_scraped_csv(existing)
    state["latest_prid"] = end_prid
    state["oldest_prid_walked"] = min(start_prid, state.get("oldest_prid_walked", start_prid))
    state["last_walk_at"] = datetime.now().isoformat(timespec="seconds")
    state["n_dates"] = len(existing)
    _write_state(state)
    log.info(
        "fbil.rbi.ingested",
        n_hits=len(hits),
        n_total_dates=len(existing),
        n_failed=n_failed,
        latest_prid=end_prid,
        oldest_prid=state["oldest_prid_walked"],
    )
    return existing


# ---------------------------------------------------------------------------
# Path 3: fallback constant (opt-in only)
# ---------------------------------------------------------------------------


def ingest_fallback_constant(start: date | None = None, end: date | None = None) -> int:
    """Write a flat FALLBACK_RATE_ANNUAL series across [start, end]. Opt-in only."""
    cfg = get_pipeline_config()
    start = start or cfg.ingest.amfi_nav.backfill_start
    end = end or date.today()
    rate_by_date = {start: FALLBACK_RATE_ANNUAL, end: FALLBACK_RATE_ANNUAL}
    series = _build_daily_series(rate_by_date, start, end)
    w.upsert_risk_free_daily(series)
    log.warning(
        "fbil.fallback_constant",
        rate=FALLBACK_RATE_ANNUAL,
        rows=series.height,
        note="Synthetic flat series — alpha/Sortino absolute values will be biased.",
    )
    return series.height


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class NoRealRiskFreeDataError(RuntimeError):
    """Raised when neither manual CSVs nor the RBI scraper produced any data."""


def ingest(
    start: date | None = None,
    end: date | None = None,
    *,
    allow_fallback: bool = False,
    years: float = 5.0,
    start_prid: int | None = None,
    end_prid: int | None = None,
) -> int:
    """Build the curated risk-free series from real sources.

    Priority:
      1. RBI press-release scraper (cached + incremental).
      2. Manual CSV files (override / merge).
      3. Fallback constant (only if allow_fallback=True).

    Manual CSV values win on date conflicts so the user can correct scraper errors.
    """
    cfg = get_pipeline_config()
    end = end or date.today()
    start = start or cfg.ingest.amfi_nav.backfill_start

    # Path 2: scrape RBI (returns dict[date, rate_pct]).
    # NOTE: IngestError from the scraper (e.g., failure-rate cap exceeded) propagates;
    # we only swallow non-pipeline exceptions so they fall through to the manual path.
    try:
        scraped = ingest_from_rbi_press_releases(
            years=years,
            start_prid=start_prid,
            end_prid=end_prid,
        )
    except IngestError:
        raise
    except Exception as e:  # noqa: BLE001
        log.error("fbil.rbi.failed", err=str(e))
        scraped = {}

    # Path 1: manual CSVs (override scraped on conflict).
    manual = _load_manual_csvs()
    rates: dict[date, float] = {**scraped, **manual}

    if not rates:
        if allow_fallback:
            log.warning("fbil.using_fallback", reason="no real data available")
            return ingest_fallback_constant(start=start, end=end)
        raise NoRealRiskFreeDataError(
            "No real T-bill data available from RBI scraper or manual CSVs. "
            "Re-run with allow_fallback=True to emit the synthetic 6.5% series."
        )

    # Freshness gate: latest observation must be within max_rf_lag_days of `end`.
    if not allow_fallback:
        latest_obs = max(rates)
        lag = (end - latest_obs).days
        max_lag = cfg.freshness.max_rf_lag_days
        if lag > max_lag:
            raise IngestError(
                f"Risk-free data stale: latest auction={latest_obs}, "
                f"lag={lag} days > max={max_lag}. RBI press-release feed may be "
                f"behind — re-run after the next weekly auction is published."
            )

    # Forward-fill across [min(rates), end].
    earliest = min(rates)
    span_start = min(start, earliest) if earliest > start else earliest
    # If user requests an `end` past the latest observation, the last observed
    # rate is forward-filled — fine for valuation purposes.
    series = _build_daily_series(rates, span_start, end)
    w.upsert_risk_free_daily(series)
    log.info(
        "fbil.ingested",
        rows=series.height,
        n_observations=len(rates),
        n_manual=len(manual),
        n_scraped=len(scraped),
        start=span_start.isoformat(),
        end=end.isoformat(),
    )
    return series.height
