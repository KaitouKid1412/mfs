"""PGIM India Mutual Fund monthly portfolio holdings adapter (Phase 5).

PGIM India publishes ONE SEBI-format ``.xlsx`` per scheme per month — the
ideal shape for ``GenericHoldingsAdapter`` + the shared ``parse_sebi_excel``
(sheet 0 carries the equity table in the standard SEBI layout: ISIN / Name /
``% to Net Assets`` header, validated below).

Discovery (Angular SPA → JSON CMS API)
--------------------------------------
The public disclosures site (``www.pgimindiamf.com``) redirects to the
parent umbrella ``www.pgimindia.com/mutual-funds``, an Angular SPA. The
monthly-portfolios route (``/mutual-funds/disclosures/Portfolios/Monthly-
Portfolio``) ships no ``.xlsx`` links in the static HTML — the list is
rendered client-side from the CMS JSON API at ``/api/v1/``. Two endpoints
drive discovery:

1. ``GET  /api/v1/brochure/disclosure/section`` → the disclosure section
   catalog. We walk it to find the ``SectionId`` whose ``SectionName`` is
   "Monthly Portfolio" (rather than hard-coding the opaque id, so a future
   CMS re-key doesn't silently break us).

2. ``POST /api/v1/brochure/published/disclosure`` with body
   ``{"sectionId": "<SectionId>"}`` → every published monthly-portfolio
   document, grouped into ``data[].content[]`` tabs (Equity / Debt / Fund
   of Funds / "Prior to June 2021"). Each item carries:
       - ``pdfPath``: the absolute file URL (despite the field name it is
         the ``.xlsx`` for the post-2021 monthlies; legacy 2021 rows are
         ``.xlsb``),
       - ``title``  : "<SCHEME NAME> <Mon> <YYYY>" (occasionally with the
         month token doubled, e.g. "… LIQUID FUND Apr 2026 Apr 2026"),
       - structured ``month`` (full English name) / ``year`` / ``date``
         fields — the **data** month-end ("30 April 2026"), which is what
         we key off (the publish date is the following month).

We filter ``content[]`` to the requested data month using the structured
``month`` + ``year`` fields (exact, locale-free), then recover the printed
scheme name by stripping the trailing "<Mon> <YYYY>" date token(s) from the
title. Files live under the CMS image path with spaces in the filename; the
absolute ``pdfPath`` is used verbatim and percent-encoded at fetch time.

Only ``sectionId`` is month-varying-free — the same POST returns the full
history, so next month works unchanged once PGIM uploads the new files.

Excel layout (validated against PGIM India Flexi Cap Fund, Apr 2026)
-------------------------------------------------------------------
- Single sheet, named after the scheme ('Flexi Cap').
- Rows 0-9: scheme banner + riskometer blurb.
- Row 10: "Portfolio Statement as on April 30, 2026".
- Row 11: header — col B='Name of Instrument', col C='ISIN',
  col D='Industry/Rating', col E='Quantity',
  col F='Market/Fair Value (INR Lacs)', col G='% to Net Assets',
  col H='% Yield'.
- Row 12+: section banners ('Equity & Equity related' / listed-on-exchange
  sub-banners) interleaved with ISIN-bearing holding rows.
- ``% to Net Assets`` is stored as a percentage (HDFC Bank = 6.39, not
  0.0639), so ``parse_sebi_excel``'s sum-based unit detector keeps it as-is.

This is the canonical SEBI layout, so ``parse_sebi_excel`` (sheet_index=0)
auto-detects the header row, the ISIN / name / weight columns, and the
weight unit — no bespoke ``parse_excel`` needed. The Flexi Cap sample yields
82 equity ISIN rows summing to 97.3%.
"""

from __future__ import annotations

import re

import httpx

from mfs.config import get_settings
from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_ORIGIN = "https://www.pgimindia.com"
_API = f"{_ORIGIN}/api/v1"
_SECTION_API = f"{_API}/brochure/disclosure/section"
_PUBLISHED_API = f"{_API}/brochure/published/disclosure"
_LISTING_PAGE = f"{_ORIGIN}/mutual-funds/disclosures/Portfolios/Monthly-Portfolio"

_MONTH_FULL = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# Month tokens that may trail the title as part of the "as on" date. We
# accept full names + the abbreviations PGIM uses ("Apr", "Sept") so the
# scheme-name stripper removes "<Mon> <YYYY>" date tails (sometimes doubled)
# without eating a genuine name token.
_DATE_MONTHS = {m.lower() for m in _MONTH_FULL} | {
    "jan", "feb", "mar", "apr", "jun", "jul", "aug",
    "sep", "sept", "oct", "nov", "dec",
}

# Strip a single trailing "<Mon> <YYYY>" group; applied repeatedly to handle
# the occasional doubled-month title ("… Apr 2026 Apr 2026").
_DATE_TAIL_RE = re.compile(r"\s+([A-Za-z]+)\s+(\d{4})$")


def _data_month_name(ym: str) -> tuple[str, str]:
    """'2026-04' → ('April', '2026'). The published-disclosure feed keys each
    item by its data-month name + year (the portfolio's "as on" month-end)."""
    y, m = map(int, ym.split("-"))
    return _MONTH_FULL[m - 1], f"{y:04d}"


def _scheme_from_title(title: str) -> str:
    """Recover the printed scheme name by stripping trailing "<Mon> <YYYY>"
    date token(s) from a published-disclosure title.

    'PGIM INDIA FLEXI CAP FUND Apr 2026' → 'PGIM INDIA FLEXI CAP FUND'.
    'PGIM INDIA LIQUID FUND Apr 2026 Apr 2026' → 'PGIM INDIA LIQUID FUND'
    (the doubled month is peeled iteratively). A trailing token that is not a
    recognized month name (e.g. the '2028' in the target-maturity index name
    'CRISIL IBX GILT INDEX - APR 2028') terminates stripping so we don't eat
    a name fragment.
    """
    s = re.sub(r"\s+", " ", title).strip()
    while True:
        m = _DATE_TAIL_RE.search(s)
        if not m or m.group(1).lower() not in _DATE_MONTHS:
            break
        s = s[: m.start()].rstrip()
    return s


def _client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(
        timeout=s.http_timeout,
        headers={
            "User-Agent": s.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": _ORIGIN,
            "Referer": _LISTING_PAGE,
        },
        follow_redirects=True,
    )


def _find_monthly_section_id(c: httpx.Client) -> str | None:
    """Resolve the "Monthly Portfolio" SectionId from the section catalog.

    Derived dynamically rather than hard-coded so a CMS re-key doesn't
    silently break discovery. Returns None if the section is absent.
    """
    r = c.get(_SECTION_API)
    r.raise_for_status()
    payload = r.json()
    for header in payload.get("data", []) or []:
        for sec in header.get("Sections", []) or []:
            name = (sec.get("SectionName") or "").strip().lower()
            if name == "monthly portfolio":
                sid = sec.get("SectionId")
                if sid:
                    return str(sid)
    return None


@register_adapter
class PgimIndiaHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "pgim_india"
    source_label = "PGIM India Mutual Fund"
    # Per-scheme files put the portfolio on sheet 0 in standard SEBI layout
    # with percentage weight units — parse_sebi_excel handles detection.
    sheet_index = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Query the monthly-portfolio CMS feed and return
        {printed_scheme_name: absolute_excel_url} for the data month ``ym``."""
        month_name, year = _data_month_name(ym)
        out: dict[str, str] = {}
        with _client() as c:
            section_id = _find_monthly_section_id(c)
            if not section_id:
                log.error("holdings.pgim_india.no_monthly_section", ym=ym)
                return {}
            r = c.post(_PUBLISHED_API, json={"sectionId": section_id})
            r.raise_for_status()
            payload = r.json()
            for tab in payload.get("data", []) or []:
                for item in tab.get("content", []) or []:
                    if str(item.get("year")) != year:
                        continue
                    if str(item.get("month") or "").strip().lower() != month_name.lower():
                        continue
                    url = item.get("pdfPath") or ""
                    # Only ISIN-bearing Excels are usable; skip the legacy
                    # .xlsb (binary) and any stray PDF rows.
                    if not url.lower().endswith((".xlsx", ".xls")):
                        continue
                    scheme = _scheme_from_title(str(item.get("title") or ""))
                    if not scheme:
                        continue
                    out.setdefault(scheme, url)
        log.info(
            "holdings.pgim_india.discover",
            ym=ym, month=month_name, year=year, n_schemes=len(out),
        )
        return out
