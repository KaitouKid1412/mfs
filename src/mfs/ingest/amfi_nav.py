"""AMFI NAV ingestion: NAVAll.txt (today) and DownloadNAVHistoryReport_Po (bulk history).

The AMFI flat-file format is pipe-delimited with interleaved AMC and scheme-type
headers — pandas/polars CSV readers cannot parse it directly. This module implements
a streaming parser that tracks the current AMC and scheme-type context and emits one
record per data row.

Endpoints:
  - https://www.amfiindia.com/spages/NAVAll.txt                  (today)
  - https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx (bulk, ≤ 90 days)
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl

from mfs import paths
from mfs.config import get_pipeline_config
from mfs.db import queries as q
from mfs.db import writers as w
from mfs.errors import IngestError
from mfs.io import http
from mfs.utils.calendar import iter_windows
from mfs.utils.logging import get_logger

log = get_logger(__name__)

NAV_TODAY_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"

DATA_HEADER_TOKENS = ("Scheme Code", "ISIN Div Payout", "Net Asset Value", "Date")


def _fmt_amfi_date(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def _parse_nav_date(s: str) -> date | None:
    s = s.strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _float_or_none(s: str) -> float | None:
    s = (s or "").strip()
    if not s or s.upper() in {"N.A.", "NA", "N/A", "-", "#N/A", "NULL"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _detect_layout(header_cols: list[str]) -> dict[str, int]:
    """Map canonical field name → column index, based on the file header row.

    Two known layouts:
      A) NAVAll.txt:  Scheme Code; ISIN Payout/Growth; ISIN Reinvestment; Scheme Name; NAV; Date  (6 cols)
      B) History:     Scheme Code; Scheme Name; ISIN Payout/Growth; ISIN Reinvestment; NAV;
                      Repurchase Price; Sale Price; Date  (8 cols)
    """
    lower = [c.lower() for c in header_cols]
    idx = {}
    for i, c in enumerate(lower):
        if "scheme code" in c:
            idx["scheme_code"] = i
        elif "scheme name" in c:
            idx["scheme_name"] = i
        elif "isin" in c and "div payout" in c or "isin growth" in c:
            idx["isin_growth"] = i
        elif "isin div reinvestment" in c:
            idx["isin_idcw"] = i
        elif c == "net asset value" or c.startswith("net asset"):
            idx["nav"] = i
        elif c == "date":
            idx["date"] = i
    return idx


def parse_amfi_text(content: str) -> Iterator[dict]:
    """Stream-parse an AMFI NAVAll or history dump.

    Field separator is `;`. The file starts with a header row that tells us the column
    layout; both the daily NAVAll and the bulk-history endpoint are supported.
    """
    current_amc: str | None = None
    current_category: str | None = None
    layout: dict[str, int] | None = None
    expected_cols: int = 0

    for raw in io.StringIO(content):
        line = raw.rstrip("\n").rstrip("\r")
        if not line.strip():
            continue
        if "Scheme Code" in line and "ISIN" in line and ";" in line:
            cols = [c.strip() for c in line.split(";")]
            layout = _detect_layout(cols)
            expected_cols = len(cols)
            continue
        if layout is None:
            continue
        if ";" in line:
            cols = [c.strip() for c in line.split(";")]
            if len(cols) >= expected_cols and cols[layout.get("scheme_code", 0)].isdigit():
                try:
                    scheme_code = cols[layout["scheme_code"]]
                    scheme_name = cols[layout["scheme_name"]]
                    isin_growth_v = cols[layout["isin_growth"]] if "isin_growth" in layout else ""
                    isin_idcw_v = cols[layout["isin_idcw"]] if "isin_idcw" in layout else ""
                    nav = _float_or_none(cols[layout["nav"]])
                    nav_date = _parse_nav_date(cols[layout["date"]])
                except (KeyError, IndexError):
                    continue
                if nav is None or nav_date is None:
                    continue
                yield {
                    "scheme_code": scheme_code,
                    "isin_growth": isin_growth_v if isin_growth_v and isin_growth_v != "-" else None,
                    "isin_idcw": isin_idcw_v if isin_idcw_v and isin_idcw_v != "-" else None,
                    "scheme_name": scheme_name,
                    "nav": nav,
                    "nav_date": nav_date,
                    "amc_name": current_amc,
                    "amfi_category": current_category,
                }
                continue
        # Header rows (no semicolon): category vs AMC vs noise.
        s = line.strip()
        if not s:
            continue
        if "Scheme" in s and ("(" in s or "-" in s):
            current_category = s
        elif s and not s[0].isdigit() and len(s) > 4 and ";" not in s and "|" not in s:
            if re.fullmatch(r"[A-Za-z0-9 .,&'()\-]+", s):
                current_amc = s


def parse_to_dataframe(content: str) -> pl.DataFrame:
    rows = list(parse_amfi_text(content))
    if not rows:
        return pl.DataFrame()
    schema = {
        "scheme_code": pl.Utf8,
        "isin_growth": pl.Utf8,
        "isin_idcw": pl.Utf8,
        "scheme_name": pl.Utf8,
        "nav": pl.Float64,
        "nav_date": pl.Date,
        "amc_name": pl.Utf8,
        "amfi_category": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema)


def fetch_today() -> bytes:
    """GET today's NAVAll.txt.

    Raises IngestError (never raw httpx errors) on 4xx or exhausted transient
    retries: this is the fetch boundary for both ingest_today and
    scheme_master.build, so a raw HTTPStatusError must not escape into either
    required pipeline stage.
    """
    try:
        return http.fetch_bytes(NAV_TODAY_URL)
    except (httpx.HTTPError, http.TransientHttpError) as e:
        raise IngestError(f"AMFI NAVAll fetch failed ({NAV_TODAY_URL}): {e}") from e


def fetch_window(from_date: date, to_date: date) -> bytes:
    params = {
        "frmdt": _fmt_amfi_date(from_date),
        "todt": _fmt_amfi_date(to_date),
    }
    return http.fetch_bytes(HISTORY_URL, params=params)


def _save_raw(content: bytes, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)
    return path


def _append_year_partition(new_rows: pl.DataFrame) -> list[int]:
    """Upsert NAV rows into Postgres (idempotent on PK scheme_code, nav_date) and
    refresh the fund_log_returns cache for affected schemes. The
    `_append_year_partition` name is retained for callsite compatibility — the
    actual partitioning is by table/PK now, not by parquet year folder."""
    if new_rows.is_empty():
        return []
    w.upsert_nav_daily(new_rows)
    years = sorted(set(int(d.year) for d in new_rows["nav_date"].to_list()))
    return years


def ingest_today() -> tuple[int, list[int]]:
    """Fetch today's NAVAll.txt, parse, append to nav_daily/. Returns (n_rows, years_touched).

    Raises IngestError if the response is empty after retries, or if the parsed snapshot
    contains zero rows (typically caused by AMFI returning an HTML error page or
    rate-limiting). Downstream stages refuse to run with stale NAVs.
    """
    try:
        content = fetch_today()
    except IngestError:
        raise  # fetch_today already translated with URL context
    except Exception as e:
        raise IngestError(f"AMFI NAVAll fetch failed after retries: {e}") from e
    today = date.today()
    _save_raw(content, paths.amfi_nav_raw(today.year, today.month, today.day))
    df = parse_to_dataframe(content.decode("utf-8", errors="replace"))
    if df.is_empty():
        log.error("amfi_nav.today.empty", bytes_received=len(content))
        raise IngestError(
            f"AMFI NAVAll returned {len(content)} bytes but parsed 0 rows. "
            f"Inspect data/raw/amfi_nav/{today:%Y/%m/%d}/NAVAll.txt — likely an HTML "
            f"error page or rate-limit response."
        )
    years = _append_year_partition(df)
    log.info("amfi_nav.today.appended", rows=df.height, years=years)
    return df.height, years


def ingest_backfill(
    start: date | None = None,
    end: date | None = None,
    *,
    fail_on_missing: bool = True,
) -> tuple[int, list[int]]:
    """Fetch full historical NAVs in bulk_window_days chunks. Idempotent.

    By default raises IngestError if any window fails all HTTP retries OR parses
    to zero rows, so daily incremental jobs don't silently proceed with holes.

    For full historical backfills (e.g., walking back to 2013), AMFI's bulk
    endpoint sometimes refuses very old windows; pass `fail_on_missing=False` to
    record those windows and continue. The list of failed windows is written to
    `data/raw/amfi_nav_history/_missing_windows.txt` for the inventory report.
    """
    cfg = get_pipeline_config()
    start = start or cfg.ingest.amfi_nav.backfill_start
    end = end or date.today()
    total_rows = 0
    years_touched: set[int] = set()
    failures: list[str] = []
    for from_d, to_d in iter_windows(start, end, cfg.ingest.amfi_nav.bulk_window_days):
        window_id = f"{from_d.isoformat()}_{to_d.isoformat()}"
        raw_path = paths.amfi_history_raw(from_d.isoformat(), to_d.isoformat())
        # Only trust the cache for windows that ended BEFORE today: a window
        # ending today is always re-fetched, so a same-day retry after a sparse
        # AMFI response doesn't replay stale bytes. Historical windows are
        # immutable and stay cached.
        if raw_path.exists() and to_d < date.today():
            content = raw_path.read_bytes()
            log.info("amfi_nav.backfill.cached", from_=from_d.isoformat(), to=to_d.isoformat())
        else:
            try:
                content = fetch_window(from_d, to_d)
            except Exception as e:  # noqa: BLE001
                log.error("amfi_nav.backfill.window_failed", window=window_id, err=str(e))
                failures.append(f"{window_id}: HTTP/{e}")
                continue
            _save_raw(content, raw_path)
        df = parse_to_dataframe(content.decode("utf-8", errors="replace"))
        if df.is_empty():
            log.error(
                "amfi_nav.backfill.window_empty",
                window=window_id,
                bytes_received=len(content),
            )
            failures.append(f"{window_id}: parsed 0 rows from {len(content)} bytes")
            continue
        years = _append_year_partition(df)
        years_touched.update(years)
        total_rows += df.height
        log.info(
            "amfi_nav.backfill.window",
            from_=from_d.isoformat(),
            to=to_d.isoformat(),
            rows=df.height,
        )
    if failures:
        missing_log = paths.raw_dir() / "amfi_nav_history" / "_missing_windows.txt"
        missing_log.parent.mkdir(parents=True, exist_ok=True)
        missing_log.write_text("\n".join(failures) + "\n")
        log.warning(
            "amfi_nav.backfill.missing_windows",
            n=len(failures),
            written_to=str(missing_log),
        )
        if fail_on_missing:
            raise IngestError(
                f"AMFI NAV backfill: {len(failures)} window(s) failed after retries:\n  - "
                + "\n  - ".join(failures)
            )
    return total_rows, sorted(years_touched)


def ingest_since(since: date) -> tuple[int, list[int]]:
    """Incremental fetch from `since` to today using bulk-history windows."""
    return ingest_backfill(start=since, end=date.today())


def ingest_incremental() -> tuple[int, list[int]]:
    """Self-healing pipeline NAV stage: heal any gap since the last run, then today.

    Reads the DB watermark MAX(nav_date) and bulk-fetches from it — INCLUSIVE,
    so a partial last day (e.g. a snapshot taken before that evening's NAVAll)
    is re-fetched and repaired — then layers today's NAVAll snapshot on top
    (the bulk endpoint may not yet include today's NAVs at run time).

    Raises IngestError on an empty nav_daily: incremental has no watermark to
    heal from, so a cold DB needs the explicit full backfill.
    """
    latest = q.latest_dates()["nav_latest"]
    if latest is None:
        raise IngestError("nav_daily empty - run mfs ingest navs --backfill")
    n1, y1 = ingest_since(latest)
    n2, y2 = ingest_today()
    return n1 + n2, sorted(set(y1) | set(y2))


def ingest_yesterday_and_today() -> tuple[int, list[int]]:
    """Convenience: fetch the last 2 calendar days via the bulk endpoint."""
    return ingest_backfill(start=date.today() - timedelta(days=1), end=date.today())
