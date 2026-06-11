"""The Wealth Company Mutual Fund monthly portfolio holdings adapter (Phase 5).

The Wealth Company (India's first woman-led AMC; SEBI final approval July
2025, first NFOs allotted 14-Oct-2025; the manager-side factsheet adapter
lives in ``mfs.ingest.managers.the_wealth_company``) publishes ONE SEBI-format
Excel per scheme per month — the ideal shape for ``GenericHoldingsAdapter`` +
the shared ``parse_sebi_excel``.

Discovery
---------
The portfolio-documents page

    https://www.wealthcompanyamc.in/literature-forms/portfolio-documents/monthly/

is a Next.js app, but its server-rendered HTML embeds every monthly-portfolio
Excel link as a plain ``<a href=...>`` followed by a ``<p>`` label of the form

    Monthly - The Wealth Company Flexi Cap Fund - April 30, 2026

so a single curl-with-browser-UA scrape gives the catalog (this is the
SBI/HDFC links-in-HTML shape, not a backing API). The most-recent ~3 months
of every scheme appear on the first (SSR) page, which always covers the latest
data month — exactly what we ingest.

CRITICAL: the data-month-end date lives in the anchor's LABEL text, NOT in the
file URL. The ``/uploads/`` filenames carry an unpredictable Strapi upload hash
and an inconsistent stem (``Monthly_Portfolio_Flexicap_<hash>.xlsx`` one month,
``Monthly_Portfolio_Flexi_Cap_<hash>.xlsx`` the next), and the SAME stem is
reused across months — so the filename can never be keyed on the data month.
We therefore key discovery off the LABEL date (``<Month> <DD>, <YYYY>``) and the
LABEL scheme name, and cache each download under a date-stamped local filename
so two months' files don't collide on disk.

Excel layout (validated against The Wealth Company Flexi Cap Fund, April 2026):
- Single sheet, named after the scheme code (e.g. 'WCFL').
- Row 2: scheme-name banner ('WCFL-THE WEALTH COMPANY FLEXI CAP FUND').
- Row 3: "Portfolio as on 30-APR-2026".
- Row 4: column header — ISIN | Name of Instrument | Rating/Industry |
  Quantity | Market Value (In Rs. lakhs) | % To Net Assets | Maturity Date |
  Put/Call Option | Yield.
- Row 5+: section banners ('EQUITY & EQUITY RELATED', '(a) Listed / awaiting
  listing on Stock Exchanges', …) interleaved with ISIN-bearing holding rows.
- '% To Net Assets' is stored as a FRACTION (0.04744 = 4.74%).

This is the canonical SEBI layout, so ``parse_sebi_excel`` (sheet_index=0)
auto-detects the header row, the ISIN/name/weight columns, and the fraction
unit (Flexi Cap April 2026: 45 equity ISIN rows summing to 91.3%, all-rows
99.8%) — no bespoke ``parse_excel`` needed.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from mfs import paths
from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import download_to, fetch_bytes
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = (
    "https://www.wealthcompanyamc.in/literature-forms/portfolio-documents/monthly/"
)

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_ALT = "|".join(_MONTH_NAMES)

# Each monthly-portfolio row in the SSR HTML is an anchor whose href is the
# /uploads/ Excel and whose immediate <p> child is the human label carrying the
# scheme name AND the data-month-end date, e.g.:
#   <a href="https://www.wealthcompanyamc.in/uploads/Monthly_Portfolio_Flexicap_00b4b2ca4d.xlsx"
#      target="_blank" rel="noopener"><p class="...">Monthly - The Wealth Company
#      Flexi Cap Fund - April 30, 2026</p>
# We key off the LABEL because the filename encodes neither the date nor a
# stable scheme spelling. Two anchors point at each file (an icon link + a
# "Download" button); only the icon link is followed by the descriptive <p>,
# so this pattern naturally keeps one row per scheme/month.
_ANCHOR_RE = re.compile(
    r'href="(?P<url>https://www\.wealthcompanyamc\.in/uploads/'
    r'[^"]+?\.xlsx?)"[^>]*>\s*<p[^>]*>\s*(?P<label>[^<]+?)\s*</p>',
    re.IGNORECASE,
)

# Parse the descriptive label into scheme name + data-month-end date:
#   "Monthly - The Wealth Company Flexi Cap Fund - April 30, 2026"
_LABEL_RE = re.compile(
    r'^\s*Monthly\s*-\s*(?P<scheme>.+?)\s*-\s*'
    rf'(?P<month>{_MONTH_ALT})\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})\s*$',
    re.IGNORECASE,
)


def _label_data_ym(month_name: str, year: str) -> str | None:
    """('April', '2026') -> '2026-04', or None if the month isn't recognized."""
    month_l = month_name.strip().lower()
    for i, n in enumerate(_MONTH_NAMES, start=1):
        if n.lower() == month_l:
            return f"{int(year):04d}-{i:02d}"
    return None


def _cache_filename(scheme_filename: str, ym: str, url: str) -> str:
    """Date-stamped local cache name so reused upstream filenames (the same
    ``Monthly_Portfolio_Flexicap_<hash>.xlsx`` stem recurs month-to-month)
    don't collide across data months. Keep the upstream extension.

    ``scheme_filename`` is what the runner passes — the printed scheme name
    with a ``.xlsx`` suffix (e.g. 'The Wealth Company Flexi Cap Fund.xlsx');
    we strip that trailing Excel extension before slugging so the cache name
    stays readable.
    """
    ext = ".xls" if unquote(url).lower().rsplit(".", 1)[-1] == "xls" else ".xlsx"
    stem = re.sub(r"\.xlsx?$", "", scheme_filename, flags=re.IGNORECASE)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_") or "scheme"
    return f"{safe}_{ym}{ext}"


@register_adapter
class TheWealthCompanyHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "the_wealth_company"
    source_label = "The Wealth Company Mutual Fund (formerly NJ/360 ONE? verify)"
    # Per-scheme files carry the portfolio on sheet 0 in standard SEBI layout.
    sheet_index = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Scrape the monthly portfolio-documents HTML and keep only the
        Excel links whose anchor-label date falls in data month ``ym``.

        Returns ``{printed_scheme_name: absolute_excel_url}`` — the printed
        name is the AMC's own label (e.g. 'The Wealth Company Flexi Cap
        Fund'), matched to scheme_master downstream.
        """
        html = fetch_bytes(_DISCLOSURE_PAGE).decode("utf-8", errors="replace")
        out: dict[str, str] = {}
        for m in _ANCHOR_RE.finditer(html):
            lm = _LABEL_RE.match(m.group("label"))
            if not lm:
                continue
            if _label_data_ym(lm.group("month"), lm.group("year")) != ym:
                continue
            scheme = re.sub(r"\s+", " ", lm.group("scheme")).strip()
            if not scheme:
                continue
            # First occurrence wins (defensive against a re-uploaded dupe).
            out.setdefault(scheme, m.group("url"))
        log.info("holdings.the_wealth_company.discover", ym=ym, n_schemes=len(out))
        return out

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Download (cached) one scheme's Excel under a date-stamped local
        name so the AMC's reused upstream filenames don't collide across
        months. ``scheme_filename`` is the printed scheme name passed by the
        runner; we derive the cache filename from it + ``ym`` + the URL."""
        local = _cache_filename(scheme_filename, ym, url)
        out = paths.holdings_excel_raw(self.amc_slug, ym, local)
        if out.exists():
            return out
        return download_to(url, out)
