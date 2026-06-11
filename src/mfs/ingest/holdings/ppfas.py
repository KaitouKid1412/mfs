"""Parag Parikh (PPFAS) monthly portfolio holdings adapter (Phase 5).

PPFAS publishes its SEBI monthly portfolio as ONE standard Excel per scheme
on a single static (server-rendered, no SPA/JS) disclosures page:

    https://amc.ppfas.com/downloads/portfolio-disclosure/

The page is a Bootstrap accordion with one collapsible card per month. Each
month's card body holds a "Consolidated" download button (a multi-scheme
``.xls`` workbook, no ``title`` attribute) followed by one per-scheme anchor
of the shape::

    <a href="/downloads/portfolio-disclosure/2026/
             PPFCF_PPFAS_Monthly_Portfolio_Report_April_30_2026.xlsx?08052026_1"
       title="Parag Parikh Flexi Cap Fund"
       class="... scheme-wise-btn" download>PPFCF ...</a>

The anchor's ``title`` attribute carries the exact printed scheme name, and
the href filename carries a scheme-code prefix (PPFCF / PPLF / PPTSF / PPCHF /
PPAF / PPDAAF / PPLCF) plus the DATA-month-end date ``<Month>_<DD>_<YYYY>``
(e.g. ``April_30_2026`` for the April-2026 data month — no publish-month
offset). The ``?<digits>_<n>`` suffix is a cache-buster and is harmless on the
fetch.

Discovery:
  - GET the disclosures page (the standard bot UA is accepted — no WAF; the
    only blocked agent was the WebFetch crawler UA, and ``/schemes/.../`` 404s
    because the real path is ``/downloads/portfolio-disclosure/``).
  - Keep per-scheme anchors (those carrying a ``title``) whose filename month
    matches the requested data month; map ``title`` -> absolute ``.xlsx`` URL.
  - The Consolidated ``.xls`` (no ``title``, multi-scheme) is skipped — our
    single-scheme ``parse_sebi_excel`` can't consume it, and every scheme is
    available individually anyway.

Per-scheme Excel layout (validated against PPFCF / PPTSF / PPDAAF / PPLCF,
April 2026):
  - One sheet, named after the scheme code (e.g. 'PPFCF').
  - row 0: scheme-name banner; row 2: "Monthly Portfolio Statement as on ...".
  - row 3: header — col B='Name of the Instrument', col C='ISIN',
    col D='Industry / Rating', col E='Quantity',
    col F='Market/Fair Value (Rs. in Lakhs)', col G='% to Net Assets',
    col H='YTM~', col I='YTC^'.
  - row 4+: section banners ('Equity & Equity related', '(a) Listed /
    awaiting listing on Stock Exchanges', Debt / Money-Market / REIT / Cash
    banners) interleaved with holding rows; '% to Net Assets' is stored as a
    FRACTION (HDFC Bank at 0.0794 == 7.94%).

This is the canonical SEBI layout, so the shared ``parse_sebi_excel`` handles
it directly: it auto-detects the header row, the ISIN/name/weight columns, and
the fraction weight unit (scaling by 100). We therefore inherit
``GenericHoldingsAdapter`` unchanged and implement only discovery.
"""

from __future__ import annotations

import re

from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import fetch_bytes
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = "https://amc.ppfas.com/downloads/portfolio-disclosure/"
_HOST = "https://amc.ppfas.com"

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Per-scheme anchor: a portfolio .xlsx whose href carries a scheme-code prefix
# and a `title="<printed scheme name>"`. The Consolidated all-schemes .xls
# button has no `title`, so this pattern naturally excludes it. We allow
# href/title in either order via two alternatives is overkill — on this page
# `href` always precedes `title`, but we keep the title match non-anchored to
# the immediate next attribute to tolerate intervening class/whitespace.
_ANCHOR_RE = re.compile(
    r'href="(?P<url>(?:https://amc\.ppfas\.com)?/downloads/portfolio-disclosure/'
    r'[^"]+?_PPFAS_Monthly_Portfolio_Report_'
    r'(?P<month>[A-Za-z]+)_(?P<day>\d{1,2})_(?P<year>\d{4})\.xlsx(?:\?[^"]*)?)"'
    r'[^>]*?\btitle="(?P<title>[^"]+)"',
    re.IGNORECASE,
)


@register_adapter
class PpfasHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "ppfas"
    source_label = "Parag Parikh Mutual Fund"

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Scrape the static disclosures page; return
        {printed_scheme_name: absolute_xlsx_url} for data month ``ym``.

        The filename date is the DATA-month end (e.g. April_30_2026 == the
        2026-04 data month), so we match month+year directly with no
        publish-month offset.
        """
        y, m = map(int, ym.split("-"))
        want_month = _MONTHS[m - 1].lower()
        want_year = f"{y:04d}"

        html = fetch_bytes(_DISCLOSURE_PAGE).decode("utf-8", errors="replace")
        import html as _html

        out: dict[str, str] = {}
        for mt in _ANCHOR_RE.finditer(html):
            if mt.group("month").lower() != want_month:
                continue
            if mt.group("year") != want_year:
                continue
            url = mt.group("url")
            if url.startswith("/"):
                url = _HOST + url
            scheme = _html.unescape(mt.group("title"))
            scheme = re.sub(r"\s+", " ", scheme).strip()
            if not scheme:
                continue
            out.setdefault(scheme, url)

        log.info(
            "holdings.ppfas.discover",
            ym=ym, month=want_month, year=want_year, n_schemes=len(out),
        )
        return out
