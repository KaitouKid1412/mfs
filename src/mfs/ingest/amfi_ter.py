"""AMFI Total Expense Ratio (TER) ingest — direct-plan monthly snapshot.

Sole source for ``scheme_ter_monthly``. AMFI publishes the SEBI-mandated TER
disclosure ("Total Expense Ratio of Mutual Fund Schemes") behind a Next.js
front-end at https://www.amfiindia.com/ter-of-mf-schemes; the page is backed
by clean JSON endpoints (the same pattern the AAUM ingester rides):

  * ``/api/populate-ter-month?year=<FY>``      → the months published in a
    financial year, e.g. ``[{"MonthYear":"May-2026","MonthNumber":"05-2026"}]``.
    The ``year`` is the Indian-FY label ``YYYY-YYYY`` (April–March).
  * ``/api/populate-te-rdata-revised?MF_ID=All&Month=<MM-YYYY>&strCat=-1&
    strType=-1&page=<n>&pageSize=<sz>`` → the disclosure rows. The response is
    ``{"data": [...], "meta": {page,pageSize,total,pageCount}}``; each row is a
    *base scheme* (no plan suffix) carrying both ``R_TER`` (regular) and
    ``D_TER`` (direct) total-expense-ratio columns, **one row per day** the
    rate was in effect that month.

We snapshot one month at a time. Because TER is disclosed daily (and may be
revised intra-month), we take, per scheme, the row with the **latest TER_Date
in the month** — the prevailing direct-plan TER at month end — and stamp it
``as_of_month`` = the first of that month.

Scheme matching: the rows print scheme NAMES, not AMFI codes, so we resolve via
the shared fuzzy matcher (``mfs.ingest._scheme_match``) against the
DIRECT+GROWTH active scheme_master — exact-canonical-first with the B5
collision guards (unique-best-within-margin, numeric-token, discriminator,
subset-poach). Canonical keys shared by ≥2 distinct funds are excluded from the
index (fail-fast: never guess a colliding key). Implausible TER (outside
``0 < ter ≤ 3.0``) is rejected — clean tuples or skip the scheme, never a
null-fill (the no-half-data invariant).

Scope is **display column + deterministic stage-2 tiebreaker only** (lower TER
breaks an exact composite tie). TER is NOT a composite-weighted score term —
weight changes are gated on the D3 backtest.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import polars as pl

from mfs.db import writers as w
from mfs.errors import IngestError
from mfs.io import http
from mfs.io.http import TransientHttpError
from mfs.ingest._scheme_match import canonicalize, match_one
from mfs.utils.logging import get_logger

log = get_logger(__name__)

MONTH_LIST_URL = "https://www.amfiindia.com/api/populate-ter-month"
DATA_URL = "https://www.amfiindia.com/api/populate-te-rdata-revised"
SOURCE_LABEL = "amfi"
PAGE_SIZE = 10000

#: Plausible direct-plan TER band. SEBI caps equity TER at 2.25% + extras;
#: direct plans run far lower. Anything outside this is a data error / a
#: close-ended or undisclosed (0.0000) row — reject per the no-half-data rule.
MIN_TER_PCT = 0.0
MAX_TER_PCT = 3.0


# ---------------------------------------------------------------------------
# Financial-year helpers
# ---------------------------------------------------------------------------


def fy_labels_around(d: date) -> list[str]:
    """The two Indian-FY labels (``YYYY-YYYY``, April–March) whose month lists
    can contain ``d``'s neighbourhood — the FY that began last April and the
    one that begins this April. Querying both and taking the max month is
    robust to AMFI's off-by-one FY bucketing of recent months."""
    return [f"{d.year - 1}-{d.year}", f"{d.year}-{d.year + 1}"]


def month_number_to_as_of(month_number: str) -> date:
    """``'05-2026'`` → ``date(2026, 5, 1)`` (first of the disclosure month)."""
    mm, yyyy = month_number.split("-", 1)
    return date(int(yyyy), int(mm), 1)


# ---------------------------------------------------------------------------
# Pure parse / match helpers (unit-tested in tests/test_amfi_ter.py — no DB,
# no network)
# ---------------------------------------------------------------------------


def clean_ter_value(raw: object) -> float | None:
    """Parse a ``D_TER`` cell to a plausible float, else None. Rejects
    non-numeric, zero/negative, and > MAX_TER_PCT (implausible for a direct
    plan) — the no-half-data guard."""
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if MIN_TER_PCT < v <= MAX_TER_PCT:
        return v
    return None


def parse_ter_rows(rows: list[dict]) -> dict[str, float]:
    """Daily disclosure rows → ``{Scheme_Name: direct_ter_pct}`` keeping, per
    scheme, the **latest TER_Date in the month** (prevailing month-end rate)
    and cleaning the direct-plan TER. Schemes whose latest value is implausible
    are dropped (never null-filled)."""
    latest: dict[str, tuple[str, object]] = {}
    for r in rows:
        name = r.get("Scheme_Name")
        ter_date = r.get("TER_Date") or ""
        if not name:
            continue
        prev = latest.get(name)
        if prev is None or ter_date > prev[0]:
            latest[name] = (ter_date, r.get("D_TER"))
    out: dict[str, float] = {}
    for name, (_, raw) in latest.items():
        v = clean_ter_value(raw)
        if v is not None:
            out[name] = v
    return out


def build_match_index(scheme_master: pl.DataFrame) -> dict[str, dict]:
    """Global DIRECT+GROWTH active candidate index ``{canonical_name:
    {scheme_code, scheme_name}}`` for fuzzy matching. Bonus-option rows are
    dropped (as in ``_scheme_match.build_candidate_index``). Canonical keys
    claimed by ≥2 distinct scheme_codes are **excluded** — a colliding key
    must never silently resolve to one of them (fail-fast / no-guessing)."""
    if scheme_master.is_empty():
        return {}
    df = scheme_master.filter(
        (pl.col("plan_type") == "DIRECT")
        & (pl.col("option_type") == "GROWTH")
        & (pl.col("is_active") == True)  # noqa: E712
        & (~pl.col("scheme_name").str.contains(r"(?i)\bBonus\b"))
    )
    codes_by_canon: dict[str, set[str]] = {}
    info: dict[str, dict] = {}
    for r in df.iter_rows(named=True):
        canon = canonicalize(r["scheme_name"])
        if not canon:
            continue
        codes_by_canon.setdefault(canon, set()).add(r["scheme_code"])
        info.setdefault(
            canon,
            {"scheme_code": r["scheme_code"], "scheme_name": r["scheme_name"]},
        )
    dropped = [c for c, codes in codes_by_canon.items() if len(codes) > 1]
    if dropped:
        log.warning("amfi_ter.match.canonical_collisions", n=len(dropped))
    return {c: info[c] for c, codes in codes_by_canon.items() if len(codes) == 1}


def match_ter_to_schemes(
    name_to_ter: dict[str, float], index: dict[str, dict], as_of_month: date,
) -> tuple[list[dict], dict[str, int]]:
    """Resolve cleaned ``{name: ter}`` to ``scheme_ter_monthly`` rows via the
    shared fuzzy matcher. Ambiguous / unmatched names are skipped (counted). If
    two distinct names resolve to the same scheme_code with *different* TERs,
    that code is dropped as ambiguous (no-guessing)."""
    now = datetime.utcnow()
    by_code: dict[str, float] = {}
    conflicts: set[str] = set()
    stats = {"matched": 0, "ambiguous": 0, "nomatch": 0}
    for name, ter in name_to_ter.items():
        res = match_one(name, index)
        if res.matched_scheme_code is None:
            stats["ambiguous" if res.ambiguous else "nomatch"] += 1
            continue
        code = res.matched_scheme_code
        if code in by_code and abs(by_code[code] - ter) > 1e-9:
            conflicts.add(code)
        by_code[code] = ter
    for code in conflicts:
        by_code.pop(code, None)
    if conflicts:
        log.warning("amfi_ter.match.code_conflicts", n=len(conflicts))
    stats["matched"] = len(by_code)
    rows = [
        {
            "scheme_code": code,
            "as_of_month": as_of_month,
            "ter_direct_pct": ter,
            "source": SOURCE_LABEL,
            "computed_at": now,
        }
        for code, ter in by_code.items()
    ]
    return rows, stats


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def _get_json(url: str, params: dict) -> object:
    try:
        raw = http.fetch_bytes(url, params=params)
    except (httpx.HTTPError, TransientHttpError) as e:
        raise IngestError(f"AMFI TER fetch failed ({url} {params}): {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise IngestError(f"AMFI TER returned non-JSON ({url} {params}): {e}") from e


def fetch_month_list(fy_label: str) -> list[dict]:
    """Months published for one Indian-FY label, newest first
    (``[{"MonthYear","MonthNumber"}]``); empty list when the FY has no data."""
    payload = _get_json(MONTH_LIST_URL, {"year": fy_label})
    return payload if isinstance(payload, list) else []


def latest_complete_month_number(today: date | None = None) -> str:
    """Discover the most recent **complete** published ``MonthNumber``
    (``'MM-YYYY'``) by scanning the FY labels around ``today``. The in-progress
    current calendar month is excluded: TER is disclosed across the month
    (republished daily), so a partial month under-covers the universe, whereas
    the latest complete month is a stable, fully-disclosed snapshot. TER moves
    slowly month-to-month, so the ~2-week recency trade-off is immaterial."""
    today = today or datetime.utcnow().date()
    seen: list[tuple[date, str]] = []
    for fy in fy_labels_around(today):
        for m in fetch_month_list(fy):
            mn = m.get("MonthNumber")
            if not mn:
                continue
            try:
                aom = month_number_to_as_of(mn)
            except (ValueError, IndexError):
                continue
            if (aom.year, aom.month) == (today.year, today.month):
                continue  # skip the in-progress (partial) current month
            seen.append((aom, mn))
    if not seen:
        raise IngestError("AMFI TER: no complete published month found in current FYs")
    return max(seen, key=lambda t: t[0])[1]


def fetch_month(month_number: str) -> list[dict]:
    """All daily disclosure rows for one month (all AMCs, all categories),
    walking every page. Raises IngestError on an empty payload."""
    rows: list[dict] = []
    page = 1
    while True:
        payload = _get_json(
            DATA_URL,
            {
                "MF_ID": "All",
                "Month": month_number,
                "strCat": "-1",
                "strType": "-1",
                "page": page,
                "pageSize": PAGE_SIZE,
            },
        )
        if not isinstance(payload, dict) or "data" not in payload:
            raise IngestError(f"AMFI TER data malformed for {month_number} (page {page})")
        rows.extend(payload["data"])
        meta = payload.get("meta") or {}
        page_count = int(meta.get("pageCount") or 1)
        if page >= page_count:
            break
        page += 1
    if not rows:
        raise IngestError(f"AMFI TER returned zero rows for {month_number}")
    return rows


def ingest_month(month_number: str | None = None) -> dict:
    """End-to-end: resolve the month (latest if None), fetch + parse + match,
    upsert ``scheme_ter_monthly``, return a summary. Fail-fast on network
    failure or a zero-row write (no half-data)."""
    from mfs.db import queries as q

    mn = month_number or latest_complete_month_number()
    as_of_month = month_number_to_as_of(mn)
    log.info("amfi_ter.fetch.start", month=mn, as_of_month=str(as_of_month))

    raw_rows = fetch_month(mn)
    name_to_ter = parse_ter_rows(raw_rows)
    index = build_match_index(q.scheme_master())
    rows, stats = match_ter_to_schemes(name_to_ter, index, as_of_month)
    if not rows:
        raise IngestError(
            f"AMFI TER {mn}: 0 schemes matched ({len(name_to_ter)} clean names, "
            f"ambiguous={stats['ambiguous']}, nomatch={stats['nomatch']})"
        )
    n = w.upsert_scheme_ter(pl.DataFrame(rows))
    log.info(
        "amfi_ter.ingest.done",
        month=mn,
        as_of_month=str(as_of_month),
        daily_rows=len(raw_rows),
        clean_names=len(name_to_ter),
        matched=stats["matched"],
        ambiguous=stats["ambiguous"],
        nomatch=stats["nomatch"],
        rows_written=n,
    )
    return {
        "month": mn,
        "as_of_month": str(as_of_month),
        "daily_rows": len(raw_rows),
        "clean_names": len(name_to_ter),
        "matched": stats["matched"],
        "ambiguous": stats["ambiguous"],
        "nomatch": stats["nomatch"],
        "rows_written": n,
    }
