"""Axis Mutual Fund monthly portfolio holdings adapter (Phase 5).

Axis publishes ONE SEBI-format Excel per scheme per month, but the
statutory-disclosures page (https://www.axismf.com/statutory-disclosures) is
a Next.js SPA — the document links are NOT in the server-rendered HTML. They
come from a JSON CMS API that the page bundle calls:

    POST https://www.axismf.com/cms/token            (no body)
        -> {"data":{"token":"Bearer <hex>", ...}}
    POST https://www.axismf.com/cms/get-scheme-documents
        Authorization: <token from /cms/token>
        body (list step):  {"sdType":"yearMonthSchemeDocs",
                            "sdID":"sdMonthSchemePortfolio"}
            -> data.schemeCategories[] = [{schemeName, schemeCode, planCode}],
               data.years[], data.months[]   (no documents)
        body (year step):  {"sdType":"yearMonthSchemeDocs",
                            "sdID":"sdMonthSchemePortfolio",
                            "year":"2026","schemeCode":"EF"}
            -> data.documentList[] = EVERY published portfolio for that scheme
               in that year, e.g. {"docuementURL":...,
               "documentName":"Monthly Portfolio - Axis Large Cap Fund -
               31 March 2026", "documentPostedDate":"2026-03-01", ...}.

IMPORTANT (2026-05 debug): the list step's ``data.months`` / ``data.years``
are STATIC calendar lists (Jan..Dec, 2012..2026) — NOT the set of months Axis
has actually published. Posting a {year, month} doc-step for a month Axis has
not yet published returns ``data.message="Data not available"`` (this is what
broke the old per-month discovery). The reliable available-months signal is
the per-scheme ``documentList`` you get back when you query with ``year``
ONLY (no ``month``): Axis returns every published portfolio for that year, and
the DATA month is the date embedded in ``documentName`` ("DD <Month> YYYY").

So discovery for ``ym`` queries by ``year`` only and matches the document
whose embedded data-month == ``ym``. If no document matches, that scheme's
``ym`` portfolio is simply not published yet (Axis publishes on a lag).

The CMS bearer token is issued anonymously by POSTing to /cms/token; no login,
cookie, or device fingerprint is required (the page's `getCMSServiceHeaders`
helper just attaches that token as `Authorization`). We mirror that exactly.

Discovery for month ``ym``:
  1. mint a token,
  2. one list-step call to enumerate every (schemeName -> schemeCode),
  3. one year-step call per UNIQUE scheme name; from its ``documentList`` pick
     the document whose embedded data-month equals ``ym`` (Axis keys documents
     by schemeCode only — plan variants share a code, and one code can back two
     name variants of the same fund; either way the URL is the fund's single
     portfolio file).
  We skip the aggregate "Consolidated" pseudo-scheme (it's the all-schemes
  workbook, which our single-scheme parser can't consume).

The data-month is the date Axis prints in ``documentName`` (files are labelled
"... 30 April 2026"), so no publish-month offset is applied.

File format quirk: Axis ships the SAME scheme as a legacy BIFF ``.xls`` (OLE2
Composite Document) in some months and an OOXML ``.xlsx`` (zip) in others
(e.g. Large Cap was .xlsx in Feb-2026 but .xls in Jan/Mar-2026). The
extension on the URL is unreliable, so we sniff the magic bytes:
  - OOXML (``PK``) -> the shared ``parse_sebi_excel`` (openpyxl) handles it;
    it auto-detects the header + the fraction weight unit.
  - BIFF (``\\xd0\\xcf\\x11\\xe0``) -> openpyxl can't read it, so we parse with
    xlrd (ABSL-style).

Per-scheme sheet layout (identical across both formats, validated against
Axis Large Cap Fund, Feb & Mar 2026):
  - One sheet (named after the internal scheme code, e.g. 'AXISEQF').
  - row 0: '<CODE> | <Scheme Name>' banner.
  - row 2: 'Monthly Portfolio Statement as on <Month DD, YYYY>'.
  - row 3: header — col B='Name of the Instrument', col C='ISIN',
    col D='Industry / Rating', col E='Quantity',
    col F='Market/Fair Value (Rs. in Lakhs)', col G='% to Net Assets',
    col H='YTM~', col I='YTC^'.
  - row 4+: section banners ('Equity & Equity related', '(a) Listed /
    awaiting listing on Stock Exchanges', 'Net Receivables / (Payables)',
    plus Debt/Money-Market/REIT banners on hybrids) interleaved with holding
    rows; then 'Sub Total' / 'Total' / 'GRAND TOTAL' footers, then YTM/YTC
    notes and risk-o-meter rows.
  - Banner / footer rows carry text in col B and an empty / non-ISIN col C.

CRITICAL UNIT NOTE: Axis stores '% to Net Assets' as a **fraction** (ICICI
Bank at 0.0884 == 8.84%), exactly like Nippon/ABSL — we multiply by 100. The
shared ``parse_sebi_excel`` already auto-detects this for the .xlsx path; the
bespoke xlrd path scales explicitly.

We hard-stop at the first ``GRAND TOTAL`` row so the trailing notes /
benchmark / risk-o-meter block can never leak in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import httpx

from mfs import paths
from mfs.ingest.holdings._generic import (
    GenericHoldingsAdapter,
    classify_section,
    is_isin,
    parse_sebi_excel,
)
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import download_to
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_HOST = "https://www.axismf.com"
_TOKEN_API = f"{_HOST}/cms/token"
_DOCS_API = f"{_HOST}/cms/get-scheme-documents"
_SD_ID = "sdMonthSchemePortfolio"
_SD_TYPE = "yearMonthSchemeDocs"

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Month name -> 1-based index, plus the common 3-letter abbreviations Axis
# sometimes prints in documentName (e.g. "31 Aug 2024").
_MONTH_INDEX: dict[str, int] = {}
for _i, _m in enumerate(_MONTHS, start=1):
    _MONTH_INDEX[_m.lower()] = _i
    _MONTH_INDEX[_m[:3].lower()] = _i
_MONTH_INDEX["sept"] = 9  # Axis occasionally writes "Sept"

# "... - 31 March 2026" / "... 30 September 25" / "... 31 Aug 2024"
# Captures the DATA month/year that Axis prints in ``documentName``.
_DOCNAME_DATE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{2,4})\s*$"
)

# The aggregate all-schemes pseudo-scheme — not a single-scheme portfolio.
_SKIP_SCHEME_NAMES = {"consolidated"}

# Stale CMS scheme-name -> current SEBI-categorised name.
#
# The Axis CMS keeps LEGACY display names in its ``schemeName`` field even
# after a SEBI recategorisation/rename, while the document URL encodes the
# CURRENT fund (verified against the filename — see below). The legacy label
# either fails the orchestrator's fuzzy match or, worse, token-set-matches the
# WRONG scheme_master row (a flagship gets silently written to a sibling fund).
# We rewrite the printed name to the current one so the shared matcher resolves
# it correctly. This is a label fix only — the underlying Excel is the named
# fund's real, unmodified portfolio:
#   * "Axis Bluechip Fund"            file=...axis_large_cap_fund...
#       (Bluechip was renamed Axis Large Cap Fund; legacy label scored 78<85
#        vs "Axis Large Cap Fund" so the flagship had no holdings).
#   * "Axis Mid Cap Fund"             file=...axis_midcap_fund...
#       (label token-set-matched "Axis Large & Mid Cap Fund" at 100, writing
#        Midcap's portfolio to the Large & Mid Cap row and starving Midcap).
#   * "Axis Growth Opportunities Fund" file=...axis_large___mid_cap_fund...
#       (renamed Axis Large & Mid Cap Fund; legacy label fuzzed to "Axis
#        Services Opportunities Fund" at 87, mis-filing the Large & Mid Cap
#        portfolio). The current Large & Mid Cap name has no CMS label of its
#        own, so this alias is what backs scheme_code 145110.
_NAME_ALIASES: dict[str, str] = {
    "Axis Bluechip Fund": "Axis Large Cap Fund",
    "Axis Mid Cap Fund": "Axis Midcap Fund",
    "Axis Growth Opportunities Fund": "Axis Large & Mid Cap Fund",
}

# Magic bytes.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy BIFF .xls
_ZIP_MAGIC = b"PK"  # OOXML .xlsx

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def _ym_year(ym: str) -> str:
    """'2026-04' -> '2026'. The Axis year-step is queried by year only; the
    target month is matched against each document's embedded data-month."""
    return f"{int(ym.split('-')[0]):04d}"


def _docname_to_ym(document_name: str) -> str | None:
    """Extract the DATA month from an Axis ``documentName`` and return it as
    ``YYYY-MM`` (or None if no trailing date is present).

    Axis labels each file with its as-on date, e.g.
    "Monthly Portfolio - Axis Large Cap Fund - 31 March 2026" -> '2026-03'.
    Handles full + 3-letter month names and 2- or 4-digit years (it has
    shipped "30 September 25" and "31 Aug 2024" historically).
    """
    if not document_name:
        return None
    m = _DOCNAME_DATE_RE.search(document_name.strip())
    if not m:
        return None
    mon_idx = _MONTH_INDEX.get(m.group(2).lower())
    if not mon_idx:
        return None
    year = int(m.group(3))
    if year < 100:  # 2-digit year like "25"
        year += 2000
    return f"{year:04d}-{mon_idx:02d}"


def _new_client() -> httpx.Client:
    return httpx.Client(
        timeout=60.0,
        follow_redirects=True,
        headers={
            "User-Agent": _UA,
            "Content-Type": "application/json",
            "Origin": _HOST,
            "Referer": f"{_HOST}/statutory-disclosures",
            "Accept": "*/*",
        },
    )


def _mint_token(client: httpx.Client) -> str:
    """POST /cms/token -> the anonymous CMS bearer token (already prefixed
    with 'Bearer ')."""
    r = client.post(_TOKEN_API, content=b"")
    r.raise_for_status()
    token = (r.json().get("data") or {}).get("token")
    if not token:
        raise RuntimeError(f"Axis /cms/token returned no token: {r.text[:200]}")
    return token


@register_adapter
class AxisHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "axis"
    source_label = "Axis Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {printed_scheme_name: absolute_excel_url} for data month
        ``ym`` by walking the Axis CMS JSON API (token -> list -> per-scheme
        year query, matching each document's embedded data-month to ``ym``).

        We query the doc-step by ``year`` ONLY rather than {year, month}: the
        per-scheme ``documentList`` it returns IS Axis's authoritative list of
        published months, and posting a month label Axis hasn't published
        returns "Data not available" (the old breakage). The target month is
        resolved by parsing the date Axis prints in each ``documentName``.
        """
        year = _ym_year(ym)
        n_offered_latest: dict[str, str] = {}  # scheme -> latest published ym
        with _new_client() as client:
            token = _mint_token(client)
            headers = {"Authorization": token}

            # Step 1: enumerate scheme name -> code (dedupe by printed name).
            list_body = {"sdType": _SD_TYPE, "sdID": _SD_ID}
            r = client.post(_DOCS_API, json=list_body, headers=headers)
            r.raise_for_status()
            data = r.json().get("data") or {}
            categories = data.get("schemeCategories") or []
            name_to_code: dict[str, str] = {}
            for c in categories:
                name = (c.get("schemeName") or "").strip()
                code = (c.get("schemeCode") or "").strip()
                if not name or not code:
                    continue
                if name.lower() in _SKIP_SCHEME_NAMES:
                    continue
                name_to_code.setdefault(name, code)

            # Step 2: one year-step lookup per unique scheme name; pick the
            # document whose embedded data-month equals ``ym``.
            out: dict[str, str] = {}
            for name, code in name_to_code.items():
                doc_body = {
                    "sdType": _SD_TYPE,
                    "sdID": _SD_ID,
                    "year": year,
                    "schemeCode": code,
                }
                try:
                    dr = client.post(_DOCS_API, json=doc_body, headers=headers)
                    dr.raise_for_status()
                    ddata = dr.json().get("data") or {}
                except Exception as e:  # noqa: BLE001 — skip a flaky scheme
                    log.warning(
                        "holdings.axis.doc_lookup_failed",
                        scheme=name, code=code, ym=ym, err=str(e),
                    )
                    continue
                doc_list = ddata.get("documentList") or []
                matched_url: str | None = None
                latest_ym: str | None = None
                for doc in doc_list:
                    doc_ym = _docname_to_ym(doc.get("documentName") or "")
                    if doc_ym is None:
                        continue
                    if latest_ym is None or doc_ym > latest_ym:
                        latest_ym = doc_ym
                    if doc_ym == ym:
                        u = (doc.get("docuementURL") or "").strip()
                        if u:
                            matched_url = u
                if latest_ym is not None:
                    n_offered_latest[name] = latest_ym
                if not matched_url:
                    # This scheme's ``ym`` portfolio is not published yet.
                    continue
                if matched_url.startswith("/"):
                    matched_url = _HOST + matched_url
                # Rewrite stale CMS labels to the current SEBI name so the
                # orchestrator's matcher resolves the flagship correctly.
                out_name = _NAME_ALIASES.get(name, name)
                out.setdefault(out_name, matched_url)

        # If nothing matched, surface the latest month Axis DOES offer so the
        # caller/operator can see the publish lag (vs. a parser/auth bug).
        latest_offered = max(n_offered_latest.values()) if n_offered_latest else None
        log.info(
            "holdings.axis.discover",
            ym=ym, year=year,
            n_schemes_listed=len(name_to_code), n_with_docs=len(out),
            latest_month_offered=latest_offered,
        )
        return out

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Download (cached) a single scheme's Excel.

        The URL extension is unreliable (Axis flip-flops between .xls BIFF and
        .xlsx OOXML), so we cache under a single canonical name and let
        ``parse_excel`` sniff the real format from the bytes.
        """
        out = paths.holdings_excel_raw(self.amc_slug, ym, scheme_filename)
        if out.exists():
            return out
        return download_to(url, out)

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def parse_excel(
        self,
        excel_path: Path,
        scheme_name_printed: str,
        ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        """Sniff BIFF vs OOXML and dispatch.

        OOXML files go through the shared ``parse_sebi_excel`` (header +
        weight-unit auto-detection). Legacy BIFF .xls files — which openpyxl
        cannot read — are parsed with xlrd against the fixed Axis column
        layout.
        """
        with open(excel_path, "rb") as fh:
            head = fh.read(8)
        if head.startswith(_ZIP_MAGIC):
            yield from parse_sebi_excel(
                excel_path, scheme_name_printed, self.amc_slug, sheet_index=0,
                expect_ym=ym,
            )
            return
        if head.startswith(_OLE2_MAGIC):
            yield from self._parse_biff(excel_path, scheme_name_printed)
            return
        log.warning(
            "holdings.axis.unknown_format",
            scheme=scheme_name_printed, path=str(excel_path),
            head=head.hex(),
        )

    def _parse_biff(
        self, excel_path: Path, scheme_name_printed: str,
    ) -> Iterable[ParsedHoldingRecord]:
        """Parse a legacy BIFF .xls Axis portfolio sheet with xlrd.

        Column layout (0-indexed): name col B(1), ISIN col C(2),
        '% to Net Assets' col G(6), stored as a FRACTION (0.0884 == 8.84%),
        so we scale by 100. Hard-stop at GRAND TOTAL.
        """
        import xlrd

        wb = xlrd.open_workbook(excel_path)
        ws = wb.sheet_by_index(0)
        current_section = "Equity"  # safe default if first banner is missing
        seen: set[str] = set()
        for r in range(ws.nrows):
            name = ws.cell_value(r, 1) if ws.ncols > 1 else None
            isin = ws.cell_value(r, 2) if ws.ncols > 2 else None
            weight = ws.cell_value(r, 6) if ws.ncols > 6 else None

            name_s = name.strip() if isinstance(name, str) else ""
            isin_s = str(isin).strip() if isin is not None else ""

            if name_s.lower() == "grand total":
                break

            # Section banner / footer: text in col B, col C not an ISIN.
            if name_s and not is_isin(isin_s):
                section = classify_section(name_s)
                if section:
                    current_section = section
                continue

            if not is_isin(isin_s) or not name_s:
                continue
            try:
                w = float(weight)
            except (TypeError, ValueError):
                continue
            if isin_s in seen:
                continue
            seen.add(isin_s)

            yield ParsedHoldingRecord(
                scheme_name_printed=scheme_name_printed,
                security_name=name_s.rstrip(" *"),
                weight_pct=w * 100.0,
                isin=isin_s,
                instrument_type=current_section,
                source_amc=self.amc_slug,
            )
