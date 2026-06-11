"""NJ Mutual Fund monthly portfolio holdings adapter (Phase 5).

NJ publishes ONE SEBI-format Excel per scheme per month — the ideal shape for
``GenericHoldingsAdapter`` + the shared ``parse_sebi_excel``.

The disclosures site (https://www.njmutualfund.com) is a React SPA whose
static HTML carries NO ``.xlsx`` links. The document catalog is served by a
JSON gateway the page bundle calls (base URL + ``/njmutualfundws``):

    GET /njmutualfundws/api/categories
        -> {"categories":[{"id":16,"name":"Financials & Portfolio"}, ...]}
    GET /njmutualfundws/api/documents/{categoryId}
        -> {"Financials & Portfolio":[{"id":127,
              "title":"Monthly Portfolio Disclosure","dispoption":"page", ...}]}
    GET /njmutualfundws/api/njmf-download?nme={documentId}
        -> {"title":"Monthly Portfolio Disclosure",
            "sections":[{"id":221,"name":"Monthly Portfolio Disclosure
              2026-2027","files":[{"label":..,"linkType":"file","url":..}]}]}

So this is the SBI/Navi discovery shape (JSON listing API), not the HDFC
links-in-HTML shape. We walk categories -> the "Monthly Portfolio
Disclosure" page document -> its sub-page sections, then pick the per-scheme
files whose URL filename carries our DATA month + year.

URL convention (verified for April 2026 data, published May 2026):
    https://www.njmutualfund.com/njmutualfundws/api/download/
        NJ-MF-Monthly-Portfolio-<CODE>-<Month>-<Year>-<uploadts>.xlsx
where ``<CODE>`` is NJ's internal scheme ticker (NJFCP, NJBAF, NJELSTCH,
NJABF, NJOVERFD) and the trailing upload timestamp is opaque (so we take the
absolute URL straight from the API rather than templating it).

NJ keys files by the DATA month (April-2026 data is labelled "April 2026"),
so there is NO publish-month offset. We filter on the URL filename's
month+year rather than the FY-section name so an April file (the first month
of a new Indian FY) is found correctly regardless of which FY section the
backend files it under.

The API ``label`` field changed format mid-history ("Monthly Portfolio -
March 31, 2026 - NJ Flexi Cap Fund" vs "NJ_MF_Monthly_Portfolio_NJFCP_April
2026"), but the internal CODE in the URL filename is stable across every
month, so we map CODE -> printed scheme name and skip label parsing
entirely. NJ runs a small, fixed scheme set; an unknown code is logged and
skipped (never guessed) so we can't fabricate a scheme name.

Excel layout (validated against NJ Flexi Cap Fund, April 2026 — 50 equity
ISIN rows summing to 97.3% to NAV):
- Single sheet named after the scheme code (e.g. 'NJFCP').
- Rows 0-2: "NJ Mutual Fund" / "<Scheme>" / scheme objective.
- Row 4: "Monthly Portfolio Statement as on ...".
- Row 5: header — Name of the Instrument | ISIN | Industry/Rating |
  Quantity | Market/Fair Value(Rs. in Lakhs) | % to Net Assets | YTM~ | YTC^.
- Row 6+: 'Equity & Equity related' / '(a) Listed / awaiting listing' section
  banners interleaved with ISIN-bearing holding rows; '% to Net Assets' is
  stored as a FRACTION (0.0452 == 4.52%).

This is the canonical SEBI layout, so ``parse_sebi_excel`` (sheet_index=0)
auto-detects the header, ISIN/name/weight columns, and the fraction unit — no
bespoke parse_excel needed.
"""

from __future__ import annotations

import json
import re

from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import fetch_bytes
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_HOST = "https://www.njmutualfund.com"
_GATEWAY = "/njmutualfundws"
_CATEGORIES_API = f"{_HOST}{_GATEWAY}/api/categories"
_DOCUMENTS_API = f"{_HOST}{_GATEWAY}/api/documents"
_NJMF_DOWNLOAD_API = f"{_HOST}{_GATEWAY}/api/njmf-download"

# Category that contains the "Monthly Portfolio Disclosure" sub-page document.
_PORTFOLIO_CATEGORY_NAME = "Financials & Portfolio"
_MONTHLY_DOC_TITLE = "Monthly Portfolio Disclosure"
# Fallback document id if the categories/documents lookup ever changes shape
# (verified: the "Monthly Portfolio Disclosure" page document is id 127).
_MONTHLY_DOC_ID_FALLBACK = 127

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Per-scheme Excel filename: NJ-MF-Monthly-Portfolio-<CODE>-<Month>-<Year>-<ts>.xlsx
_FILENAME_RE = re.compile(
    r"NJ-MF-Monthly-Portfolio-(?P<code>[A-Z0-9]+)-"
    r"(?P<month>[A-Za-z]+)-(?P<year>\d{4})-\d+\.xlsx$",
    re.IGNORECASE,
)

# NJ's internal scheme ticker -> printed scheme name (matches scheme_master).
# NJ runs a small, stable line-up; we map codes explicitly rather than
# parsing the inconsistent API ``label`` text. An unknown code is skipped
# (logged) so a new scheme can never be silently mis-attributed.
_CODE_TO_SCHEME = {
    "NJBAF": "NJ Balanced Advantage Fund",
    "NJELSTCH": "NJ ELSS Tax Saver Scheme",
    "NJFCP": "NJ Flexi Cap Fund",
    "NJABF": "NJ Arbitrage Fund",
    "NJOVERFD": "NJ Overnight Fund",
}


def _data_month_tokens(ym: str) -> tuple[str, str]:
    """'2026-04' -> ('April', '2026'). NJ keys files by the DATA month, so no
    publish-month offset."""
    y, m = map(int, ym.split("-"))
    return _MONTH_NAMES[m - 1], f"{y:04d}"


def _resolve_monthly_doc_id() -> int:
    """Look up the "Monthly Portfolio Disclosure" page document id via the
    categories + documents APIs, falling back to the known id if the catalog
    layout shifts."""
    try:
        cats = json.loads(fetch_bytes(_CATEGORIES_API)).get("categories") or []
        cat_id = next(
            (c["id"] for c in cats
             if (c.get("name") or "").strip() == _PORTFOLIO_CATEGORY_NAME),
            None,
        )
        if cat_id is not None:
            docs = json.loads(fetch_bytes(f"{_DOCUMENTS_API}/{cat_id}"))
            for doc_list in docs.values():
                for doc in doc_list or []:
                    if (doc.get("title") or "").strip() == _MONTHLY_DOC_TITLE:
                        return int(doc["id"])
    except Exception as e:  # noqa: BLE001 — degrade to the known id, don't crash
        log.warning("holdings.nj.doc_id_lookup_failed", err=str(e))
    return _MONTHLY_DOC_ID_FALLBACK


@register_adapter
class NjHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "nj"
    source_label = "NJ Mutual Fund"
    # Per-scheme files put the portfolio on sheet 0 in standard SEBI layout.
    sheet_index = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Walk the NJ JSON catalog and return {printed_scheme_name:
        absolute_xlsx_url} for data month ``ym``."""
        month_name, year = _data_month_tokens(ym)
        doc_id = _resolve_monthly_doc_id()

        payload = json.loads(fetch_bytes(f"{_NJMF_DOWNLOAD_API}?nme={doc_id}"))
        sections = payload.get("sections") or []

        out: dict[str, str] = {}
        for sec in sections:
            for f in sec.get("files") or []:
                if (f.get("linkType") or "") != "file":
                    continue
                url = (f.get("url") or "").strip()
                if not url:
                    continue
                if url.startswith("/"):
                    url = _HOST + url
                filename = url.rsplit("/", 1)[-1].split("?", 1)[0]
                m = _FILENAME_RE.search(filename)
                if not m:
                    continue
                # Filter on the file's own DATA month + year (robust across FY
                # section boundaries — an April file may sit under a new FY).
                if (
                    m.group("month").lower() != month_name.lower()
                    or m.group("year") != year
                ):
                    continue
                code = m.group("code").upper()
                scheme = _CODE_TO_SCHEME.get(code)
                if not scheme:
                    log.warning(
                        "holdings.nj.unknown_code",
                        code=code, filename=filename, ym=ym,
                    )
                    continue
                out.setdefault(scheme, url)

        log.info("holdings.nj.discover", ym=ym, month=month_name, year=year,
                 doc_id=doc_id, n_schemes=len(out))
        return out
