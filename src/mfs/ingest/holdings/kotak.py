"""Kotak Mahindra Mutual Fund monthly portfolio holdings adapter (Phase 5).

Kotak's portfolios page (https://www.kotakmf.com/Information/portfolios) is an
Angular SPA whose entire HTML origin sits behind Radware Bot Manager — a plain
``curl`` of any page (or of ``/api/...``) is 302-redirected to a
``validate.perfdrive.com`` bot challenge. The SPA itself, however, talks to a
JSON API on a DIFFERENT host that is NOT bot-protected:

    https://vlbapiprodtest.kotakmf.com/kotakapi/portfolio/...

(this is ``urlProxies.kotakapi`` in the bundle; the www.kotakmf.com ``/api``
path is just a reverse-proxy of the same service.)

Discovery + download mirror the SPA's calls (reverse-engineered from the
page's lazy chunk ``194.js``):

  1. schemesearch/<query>             -> {"fundList":[{"FundName","FundCode"}]}
  2. folderlist?scheme=<code>/<year>/<month>/Consolidated -> {"dataList":[<file>]}
  3. downloadfile?path=<code>/<year>/<month>/Consolidated&filename=<file>

The month-end ("whole month") snapshot lives under the ``Consolidated`` range;
the leaf ``dataList[0]`` is the bare Excel filename, which is ALWAYS
``<FundCode>.xlsx`` (verified across the whole catalog: SEF.xlsx, KOP.xlsx,
CON_.xlsx, ...). So we construct both the leaf-listing URL and the download URL
DIRECTLY from the fund code at discovery time — no per-scheme folderlist call —
which makes discovery a single ``schemesearch`` request.

THREE non-obvious failure modes drove the rewrite of the old adapter (which
discovered 92 names but landed only ~15, covering 6/29 ranked equity funds):

* The intermediate ``folderlist?scheme=<code>/<year>/<month>`` date-ranges
  listing (which the SPA uses to enumerate ranges like ``["Consolidated"]``)
  returns ``{"status":"Failure","statusCode":"400"}`` DETERMINISTICALLY for a
  large subset of schemes — including most ranked equity funds (KOP, MUC, ELS,
  KFE, KBC, CMP, KMN, ...). The old adapter gated on ``"Consolidated" in <that
  listing>`` and so silently dropped every such scheme. We never call that
  level: the ``/<month>/Consolidated`` leaf works even when the range listing
  400s. Since we derive the filename from the code, we don't even need the leaf
  listing for discovery — but ``fetch_excel`` still issues it (see below).

* The prodtest host (an AWS load balancer) throws genuinely-TRANSIENT 400s on
  ``folderlist`` and ``downloadfile`` from some backend instances; they
  self-heal within a few seconds. ``schemesearch`` retries via ``_get_json``;
  ``fetch_excel`` retries the whole prime+download attempt (see below).

* CRITICAL: ``downloadfile`` only succeeds on the SAME backend instance that
  served a preceding ``folderlist`` for that path — i.e. it requires the
  ``AWSALB`` SESSION-AFFINITY cookie set by that folderlist response. A
  download issued from a fresh client (no cookie) 400s every time. The
  inherited ``GenericHoldingsAdapter.fetch_excel`` uses ``io.http.download_to``,
  which spins up a fresh cookie-less client per call — that is why the old
  adapter's downloads 400'd. We override ``fetch_excel`` to do, per attempt:
  CLEAR the cookie jar (so the LB re-routes off any stuck backend), hit the
  leaf folderlist to pin a fresh backend + set its cookie, then download on
  that same backend — retrying the whole attempt. Clearing the cookie between
  attempts is essential: retrying with a cookie pinned to a stuck backend can
  never recover, which is what made flaky-but-real ranked funds 400 out.

Data month ``2026-04`` is filed under the literal month folder ``April`` (the
data-month, not the May publish month).

We enumerate the full equity-fund universe by issuing schemesearch for a small
set of generic equity prefixes ("kotak ...") and de-duplicating by FundCode;
schemesearch is a substring match, so "kotak" alone returns the whole list.

Excel layout (validated against Kotak Flexicap Fund "SEF", April 2026):
- One sheet per file, named after the scheme code.
- Row 0: "Portfolio of <Scheme>" banner.
- Row 1: header — col A "Name of Instrument" (a MERGED cell spanning cols
  A-C), col D "ISIN Code", col E "Industry", col I "% to Net Assets".
- Section banners ("Equity & Equity related", "Listed/Awaiting listing...",
  "Mutual Fund Units", "Unlisted", "Total", "Triparty Repo") appear in col A,
  B, or C with the rest of the row empty.
- Holding rows: name in col C (index 2), ISIN in col D (index 3), weight
  ("% to Net Assets", already a percent) in col I (index 8).
- A trailing "Nav Details" / derivative-exposure block after the portfolio
  puts NAV floats in the ISIN column — those are filtered by the strict ISIN
  regex, so we don't need an explicit stop, but we hard-stop at the first
  "Grand Total" for safety.

Because the header "Name of Instrument" is a merged cell that openpyxl reports
only in col A while the data names live in col C, the generic
``parse_sebi_excel`` header auto-detect picks the wrong name column and yields
zero rows. We therefore override ``parse_excel`` with Kotak's fixed column
offsets (name=2, ISIN=3, weight=8).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import openpyxl

from mfs import paths
from mfs.errors import IngestError
from mfs.ingest.holdings._generic import (
    GenericHoldingsAdapter,
    classify_section,
    is_isin,
)
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.content_check import sniff_kind
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

# Non-bot-protected JSON API host the SPA actually calls (urlProxies.kotakapi).
# This host is fully open — no Radware challenge, any User-Agent works — unlike
# the www.kotakmf.com origin (and its /api reverse-proxy), which 302s every
# request to a perfdrive.com bot challenge. So both discovery (this JSON API)
# and download (downloadfile endpoint, inherited GenericHoldingsAdapter.
# fetch_excel via the standard cached download) need no special headers.
_API = "https://vlbapiprodtest.kotakmf.com/kotakapi/portfolio"

_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# The month-end ("whole month") snapshot is filed under this folderlist range.
# Intra-month ranges ("1 to 15", "16 to 30", ...) are partial and not what we
# want; "Consolidated" is the SEBI month-end portfolio.
_FULL_MONTH_RANGE = "Consolidated"

# Fixed column offsets in Kotak's per-scheme sheet (0-indexed).
_COL_NAME = 2
_COL_ISIN = 3
_COL_WEIGHT = 8

# The vlbapiprodtest host is openly reachable but FLAKY: the SAME folderlist
# call that returns a populated ``dataList`` one second returns
# ``{"status":"Failure","statusCode":"400"}`` the next (the service sits behind
# an AWS load balancer with some backend instances intermittently erroring).
# These leaf-level 400s ARE transient and self-heal within several seconds —
# distinct from the date-range ``/<month>`` listing's DETERMINISTIC 400 (see
# module docstring), which we avoid calling entirely. A silent miss would drop
# a real equity scheme from the universe (violating the pipeline's fail-fast
# "never proceed with partial inputs" rule), so every folderlist/schemesearch
# call retries with a short backoff until it gets a 200 + well-formed JSON.
# Real-but-flaky schemes can need many leaf-prime attempts before the AWS load
# balancer routes them to a healthy backend (observed: ranked equity funds like
# Kotak Focused Fund still 400 across ~8 attempts before self-healing). A
# genuinely-absent scheme 400s on EVERY attempt, so a generous budget only
# costs time on schemes we've FAILED to name-filter below — of which there are
# none after the closed-series filter. We therefore keep the budget high so we
# never drop a real fund to transient flakiness (the fail-fast invariant:
# better to spend seconds retrying than silently lose a ranked scheme).
_MAX_RETRIES = 20
_RETRY_SLEEP_S = 1.5

# --- Absent-month probe + TTL'd negative cache (B11) ------------------------
#
# The generous per-scheme retry budget above is correct for a PUBLISHED month
# (real flakiness needs it) but pathological for an UNPUBLISHED one: during
# the first ~10 days of a month every one of ~120 schemes burns the full
# 20 x 1.5s budget — ~2.6h of guaranteed-dead retries per run. Before building
# any download URL, discovery probes whether the month exists at all: TWO
# stable flagship fund codes (two, so one delisted fund can't false-negative)
# each get a SMALL folderlist budget. The transient-400 flakiness self-heals
# within seconds, so 5 attempts x 1.5s per code is ample for a published
# month; a truly absent month 400s deterministically on every attempt. If
# BOTH probes exhaust their budget, we write a TTL'd negative-cache marker
# (data/raw/holdings/kotak/<ym>/.month_absent, ISO-8601 UTC timestamp inside)
# and raise IngestError — the holdings orchestrator's per-AMC isolation
# records the gap and the run continues (no half-data: the AMC is skipped
# entirely). While the marker is younger than the TTL, subsequent runs raise
# immediately with ZERO HTTP calls; an older marker is deleted and the month
# re-probed, so a newly published month is picked up at most ~a day late and
# the marker clears itself naturally.
_PROBE_RETRIES = 5
_NEG_CACHE_TTL_H = 20.0
_ABSENT_MARKER_NAME = ".month_absent"
# Flagship codes preferred for the probe (Flexicap and Large & Midcap —
# long-lived open-end equity funds verified to publish a Consolidated
# portfolio every month). If either is missing from the catalog we fall back
# to the catalog's first codes in deterministic (fund-name-sorted) order.
_PROBE_PREFERRED_CODES = ("SEF", "KOP")

# Closed-end series schemes — Fixed Maturity Plans ("Kotak FMP Series 237",
# "Kotak Fixed Maturity Plan Series 330") and the legacy "Kotak India Growth
# Fund Series N" — do NOT publish a monthly equity-style Consolidated portfolio
# (their ``/<month>/Consolidated`` leaf 400s DETERMINISTICALLY). They are never
# in the ranked DIRECT+GROWTH equity universe, but their printed names
# fuzzy-match ranked funds at the matcher's threshold, so leaving them in
# discovery would make the orchestrator attempt ~71 dead downloads, each
# burning the full (now-generous) retry budget. We drop them at discovery —
# this is NOT dropping real equity; these are closed-end debt / wound-down
# series with no monthly portfolio to fetch.
_CLOSED_SERIES_RE = re.compile(
    r"(?i)\b(?:"
    r"fmp|fixed\s+maturity\s+plan"        # FMP series (debt, closed-end)
    r"|india\s+growth\s+fund\s+series"    # legacy closed India Growth series
    r")\b"
)

# Discovery-name normalisation for three ranked equity funds whose Kotak-printed
# name canonicalises to a token set that the shared scheme_match fuzzy matcher
# (token_set_ratio + Levenshtein tie-break — a file we must not edit) resolves
# to the WRONG scheme_master sibling. The matcher input is the printed name, so
# the only lever we own is what discovery emits. Each rewrite nudges the printed
# name's canonical form toward the true scheme_master canonical key so the
# tie-break lands on the right scheme_code — WITHOUT inventing data (the
# download URL is keyed off the FundCode, which is unchanged):
#
#   * "Kotak Smallcap Fund" (code MID) — canon "KOTAK SMALLCAP FUND" token-set-
#     matches the Nifty Smallcap *index* funds (which share the one-word
#     "SMALLCAP" token) at 100 over the true active Small Cap fund whose master
#     name is "Kotak-Small Cap Fund" → "KOTAK SMALL CAP FUND" (two tokens).
#     Splitting "Smallcap"→"Small Cap" aligns the tokens so the active fund wins.
#   * "Kotak Large And Midcap Fund" (code KOP) — canon "KOTAK LARGE AND MIDCAP
#     FUND" is a superset of "KOTAK MIDCAP FUND", which ties at token_set_ratio
#     100 and wins the Levenshtein tie-break on the shorter string. The master
#     row's suffix "- Direct- Growth" (no "Plan") is not stripped by the shared
#     canonicaliser, leaving a trailing "DIRECT" token in the true key
#     ("KOTAK LARGE MIDCAP FUND DIRECT"); appending "Direct" to the printed name
#     restores that token so the tie-break favours the true L&M fund (120158).
#   * "Kotak Services Fund" (code SRF) — same trailing-"DIRECT" asymmetry: the
#     master row "Kotak Services Fund - Direct - Growth" canonicalises to
#     "KOTAK SERVICES FUND DIRECT"; without the "Direct" token the printed name
#     ties against several "... FINANCIAL SERVICES ..." index funds and the
#     tie-break mis-fires. Appending "Direct" yields an exact-length match.
_DISCOVERY_NAME_REWRITES = {
    "Kotak Smallcap Fund": "Kotak Small Cap Fund",
    "Kotak Large And Midcap Fund": "Kotak Large And Midcap Fund Direct",
    "Kotak Large and Midcap Fund": "Kotak Large And Midcap Fund Direct",
    "Kotak Services Fund": "Kotak Services Fund Direct",
}


def _month_name(ym: str) -> str:
    """data_ym='2026-04' -> 'April' (Kotak files the month-end snapshot under
    the data-month folder, not the publish month)."""
    _, m = map(int, ym.split("-"))
    return _MONTH_NAMES[m - 1]


def _year(ym: str) -> str:
    return ym.split("-", 1)[0]


def _absent_marker_path(ym: str) -> Path:
    """TTL'd negative-cache marker for an unpublished data month."""
    return paths.raw_dir() / "holdings" / "kotak" / ym / _ABSENT_MARKER_NAME


def _absent_marker_age_hours(marker: Path) -> float | None:
    """Age of the marker in hours, or None if it doesn't exist.

    The marker body is the ISO-8601 UTC timestamp written at probe time; an
    unparseable body (manual tampering, partial write) is treated as expired
    so we re-probe rather than trusting a corrupt marker.
    """
    try:
        text = marker.read_text().strip()
    except OSError:
        return None
    try:
        written = _dt.datetime.fromisoformat(text)
    except ValueError:
        return float("inf")
    if written.tzinfo is None:
        written = written.replace(tzinfo=_dt.timezone.utc)
    age = _dt.datetime.now(_dt.timezone.utc) - written
    return age.total_seconds() / 3600.0


def _month_not_published_error(ym: str) -> IngestError:
    return IngestError(f"kotak: month {ym} not yet published (probe negative)")


def _get_json(client: httpx.Client, url: str) -> dict | None:
    """GET a Kotak portfolio JSON endpoint, retrying through the prodtest
    host's transient failures.

    Returns the parsed JSON object with ``status == "Success"``, or None only
    after ``_MAX_RETRIES`` attempts all fail. A genuine "no data" answer from
    Kotak is still a 200 + Success JSON (with an empty ``dataList``), so the
    caller can safely treat None as a hard fetch failure rather than as
    "scheme has no portfolio".
    """
    for attempt in range(_MAX_RETRIES):
        try:
            r = client.get(url)
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code == 200:
            try:
                obj = json.loads(r.text)
            except (json.JSONDecodeError, ValueError):
                obj = None
            # Accept any well-formed JSON dict; "Success" is the happy path but
            # some leaf calls omit the status field while still carrying data.
            if isinstance(obj, dict) and (
                obj.get("status") == "Success"
                or "dataList" in obj
                or "fundList" in obj
            ):
                return obj
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_SLEEP_S)
    log.warning("holdings.kotak.fetch_failed", url=url, attempts=_MAX_RETRIES)
    return None


@register_adapter
class KotakHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "kotak"
    source_label = "Kotak Mahindra Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {printed_scheme_name: absolute downloadfile URL} for the
        month-end (Consolidated) portfolio of every scheme in the catalog.

        Discovery is a SINGLE ``schemesearch`` call. The Consolidated leaf
        filename is always ``<FundCode>.xlsx`` and the download path is a
        fixed template, so we build every URL directly from the code rather
        than per-scheme folderlist drill-downs (which are slow + flaky). A
        scheme with no portfolio for this month simply 400s at download time
        (after retries) and is skipped by the orchestrator — so we do not
        gate per scheme, which keeps the full catalog in the matchable set.

        We DO gate on the month existing at all: an unpublished month would
        otherwise burn the full per-scheme retry budget ~120 times (~2.6h of
        dead retries). See ``_probe_month_published`` and the negative-cache
        notes at ``_PROBE_RETRIES``. A fresh ``.month_absent`` marker raises
        ``IngestError`` immediately, with zero HTTP traffic.
        """
        self._check_absent_marker(ym)
        year = _year(ym)
        month = _month_name(ym)
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            funds = self._all_funds(client)
            self._probe_month_published(client, funds, ym, year, month)
        out: dict[str, str] = {}
        for fund_name, code in funds.items():
            out.setdefault(fund_name, self._download_url(code, year, month))
        log.info(
            "holdings.kotak.discover",
            ym=ym, year=year, month=month, n_schemes=len(out),
        )
        return out

    # ------------------------------------------------------------------
    # Absent-month probe + negative cache (B11)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_absent_marker(ym: str) -> None:
        """Raise immediately (zero HTTP) while a young negative-cache marker
        exists; delete an expired marker so the month is re-probed."""
        marker = _absent_marker_path(ym)
        age_h = _absent_marker_age_hours(marker)
        if age_h is None:
            return
        if age_h < _NEG_CACHE_TTL_H:
            log.info(
                "holdings.kotak.month_absent_cached",
                ym=ym, marker=str(marker), age_hours=round(age_h, 2),
            )
            raise _month_not_published_error(ym)
        marker.unlink(missing_ok=True)
        log.info("holdings.kotak.month_absent_marker_expired", ym=ym)

    def _probe_month_published(
        self,
        client: httpx.Client,
        funds: dict[str, str],
        ym: str,
        year: str,
        month: str,
    ) -> None:
        """Probe two stable fund codes' Consolidated leaves for this month.

        One probe succeeding proves the month is published (we return and
        the normal per-scheme path runs with its generous retry budget).
        BOTH probes exhausting their SMALL budget means the month is not
        published: write the TTL'd marker and raise ``IngestError`` so the
        orchestrator records the gap and skips kotak entirely this run.
        """
        codes: list[str] = [
            c for c in _PROBE_PREFERRED_CODES if c in funds.values()
        ]
        for name in sorted(funds):
            if len(codes) >= 2:
                break
            if funds[name] not in codes:
                codes.append(funds[name])
        if not codes:
            return  # empty catalog: nothing to probe (discovery yields {})

        for code in codes[:2]:
            leaf_url = f"{_API}/folderlist?scheme={self._path(code, year, month)}"
            for attempt in range(_PROBE_RETRIES):
                client.cookies.clear()  # re-route off any stuck LB backend
                try:
                    r = client.get(leaf_url)
                except httpx.HTTPError:
                    r = None
                if r is not None and r.status_code == 200:
                    try:
                        obj = json.loads(r.text)
                    except (json.JSONDecodeError, ValueError):
                        obj = None
                    files = (obj or {}).get("dataList") or []
                    if files and files[0]:
                        # Month is published; clear any leftover marker.
                        _absent_marker_path(ym).unlink(missing_ok=True)
                        return
                if attempt < _PROBE_RETRIES - 1:
                    time.sleep(_RETRY_SLEEP_S)

        marker = _absent_marker_path(ym)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(_dt.datetime.now(_dt.timezone.utc).isoformat())
        log.warning(
            "holdings.kotak.month_absent",
            ym=ym, probed_codes=codes[:2],
            ttl_hours=_NEG_CACHE_TTL_H, marker=str(marker),
        )
        raise _month_not_published_error(ym)

    def _all_funds(self, client: httpx.Client) -> dict[str, str]:
        """Enumerate {FundName: FundCode} via schemesearch.

        schemesearch is a case-insensitive substring match over fund names.
        Every Kotak scheme name begins with "Kotak", so the single query
        "kotak" returns the entire catalog (verified: it is a strict superset
        of narrower queries like "fund", and contains no non-Kotak AMC names).
        We de-duplicate by name (the raw list repeats each fund once per
        share-class/plan).
        """
        out: dict[str, str] = {}
        data = _get_json(client, f"{_API}/schemesearch/kotak")
        for f in (data or {}).get("fundList", []) or []:
            name = (f.get("FundName") or "").strip()
            code = (f.get("FundCode") or "").strip()
            if not name or not code or name.lower() == "no data":
                continue
            # Closed-end series schemes (FMP / legacy India Growth series)
            # publish no monthly Consolidated portfolio (deterministic leaf
            # 400) yet fuzzy-match ranked funds — skip them so we don't burn
            # the retry budget on dead downloads.
            if _CLOSED_SERIES_RE.search(name):
                continue
            # Nudge the three mis-matching ranked equity names toward their
            # true scheme_master canonical form (see _DISCOVERY_NAME_REWRITES).
            # Keyed off the raw FundName; the FundCode (hence download URL) is
            # untouched, so this changes only how the matcher reads the name.
            name = _DISCOVERY_NAME_REWRITES.get(name, name)
            out.setdefault(name, code)
        return out

    @staticmethod
    def _path(code: str, year: str, month: str) -> str:
        return f"{code}/{year}/{month}/{_FULL_MONTH_RANGE}"

    def _download_url(self, code: str, year: str, month: str) -> str:
        """Construct the Consolidated download URL directly from the code.

        The leaf filename is ALWAYS ``<code>.xlsx`` (verified across the whole
        catalog), so no folderlist call is needed at discovery time.
        """
        path = self._path(code, year, month)
        return f"{_API}/downloadfile?path={path}&filename={code}.xlsx"

    # ------------------------------------------------------------------
    # Download — bespoke because (1) the prodtest host throws transient 400s
    # that need retrying and (2) downloadfile REQUIRES the AWSALB
    # session-affinity cookie set by a preceding folderlist on the same path,
    # so a fresh cookie-less client (what the inherited fetch_excel uses)
    # always 400s. We reuse one persistent client and prime the session with
    # the leaf folderlist before downloading.
    # ------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        c = getattr(self, "_http", None)
        if c is None:
            # Tight per-request timeout (a transient-bad backend usually
            # 400s instantly, but can also hang; we want to fail fast into
            # the retry rather than block on one slow socket). Generous read
            # timeout for the actual ~160 KB xlsx body.
            c = httpx.Client(
                timeout=httpx.Timeout(connect=10.0, read=30.0,
                                      write=10.0, pool=10.0),
                follow_redirects=True,
            )
            self._http = c
        return c

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        out = paths.holdings_excel_raw(self.amc_slug, ym, scheme_filename)
        if out.exists():
            return out

        # Reconstruct the folderlist (leaf) URL for this download's path. The
        # download URL carries ?path=<code>/<year>/<month>/Consolidated.
        qs = parse_qs(urlparse(url).query)
        path = (qs.get("path") or [""])[0]
        leaf_url = f"{_API}/folderlist?scheme={path}"
        client = self._client()

        # Each attempt is a self-contained leaf-prime + download on a FRESHLY
        # routed backend. The AWSALB cookie pins us to ONE load-balancer
        # backend, so if that backend is in its intermittent-400 state, no
        # number of retries on the SAME cookie will recover. We therefore
        # CLEAR the cookie jar before every attempt: the LB re-routes us to a
        # (possibly different, possibly healthy) backend, the leaf-prime sets a
        # fresh sticky cookie, and the download immediately rides the same
        # freshly-pinned backend. This is what makes flaky-but-real schemes
        # (e.g. Kotak Focused Fund) reliably resolve.
        for attempt in range(_MAX_RETRIES):
            client.cookies.clear()
            data = self._prime_and_download(client, leaf_url, url)
            if data is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.with_suffix(out.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.rename(out)
                return out
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_SLEEP_S)

        # Every attempt failed. A genuinely-absent portfolio (closed scheme not
        # caught by the name filter) and a persistently-flaky backend are
        # indistinguishable here; raising lets the orchestrator skip the
        # scheme. The leaf-level filter (_CLOSED_SERIES_RE) keeps the known
        # no-portfolio schemes out, so a raise here is almost always a real
        # fetch failure worth surfacing.
        log.warning(
            "holdings.kotak.download_failed", url=url, attempts=_MAX_RETRIES,
        )
        raise RuntimeError(
            f"kotak: leaf-prime+download failed after {_MAX_RETRIES} "
            f"attempts for path={path!r}"
        )

    def _prime_and_download(
        self, client: httpx.Client, leaf_url: str, download_url: str,
    ) -> bytes | None:
        """One attempt: prime the session via the leaf folderlist (to pin a
        backend + set the AWSALB cookie), then download on the same backend.
        Returns the xlsx bytes, or None if either step failed this attempt.
        """
        # Leaf-prime: a single GET (the outer loop owns the retry/backoff).
        try:
            lr = client.get(leaf_url)
        except httpx.HTTPError:
            return None
        if lr.status_code != 200:
            return None
        try:
            obj = json.loads(lr.text)
        except (json.JSONDecodeError, ValueError):
            return None
        files = (obj or {}).get("dataList") or []
        if not files or not files[0]:
            return None
        # Download on the SAME client (same freshly-set AWSALB cookie).
        try:
            dr = client.get(download_url)
        except httpx.HTTPError:
            return None
        # downloadfile returns the raw xlsx (PK\x03\x04 zip magic). Anything
        # else — a JSON error body, an HTML challenge/WAF page served as 200 —
        # counts as a FAILED attempt (the outer loop retries) and is never
        # written to the cache (B1 content validation).
        if dr.status_code == 200 and sniff_kind(dr.content) == "xlsx_zip":
            return dr.content
        return None

    # ------------------------------------------------------------------
    # Parse — bespoke because the "Name of Instrument" header is a merged
    # cell (col A) while the data names live in col C, which defeats the
    # generic header auto-detect.
    # ------------------------------------------------------------------

    def parse_excel(
        self, excel_path: Path, scheme_name_printed: str, ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        try:
            ws = wb[wb.sheetnames[0]]
            current_section = "Equity"
            seen: set[str] = set()
            ncols = max(_COL_NAME, _COL_ISIN, _COL_WEIGHT) + 1
            for row in ws.iter_rows(values_only=True):
                cells = list(row) + [None] * max(0, ncols - len(row))
                name = cells[_COL_NAME]
                isin = cells[_COL_ISIN]
                weight = cells[_COL_WEIGHT]

                name_s = name.strip() if isinstance(name, str) else ""
                isin_s = str(isin).strip() if isin is not None else ""

                # Section banners live in col A / B / C with no ISIN. Scan the
                # leading text cells for a recognizable banner.
                if not is_isin(isin_s):
                    banner = ""
                    for c in cells[: _COL_NAME + 1]:
                        if isinstance(c, str) and c.strip():
                            banner = c.strip()
                            break
                    if banner.lower() in ("grand total", "total portfolio"):
                        break
                    section = classify_section(banner)
                    if section:
                        current_section = section
                    continue

                if not name_s or isin_s in seen:
                    continue
                try:
                    w = float(weight)
                except (TypeError, ValueError):
                    continue
                seen.add(isin_s)
                yield ParsedHoldingRecord(
                    scheme_name_printed=scheme_name_printed,
                    security_name=name_s.rstrip(" *"),
                    weight_pct=w,
                    isin=isin_s,
                    instrument_type=current_section,
                    source_amc=self.amc_slug,
                )
        finally:
            wb.close()
