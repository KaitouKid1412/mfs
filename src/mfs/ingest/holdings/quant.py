"""quant Mutual Fund monthly portfolio holdings adapter (Phase 5).

quant publishes ONE SEBI-format Excel per scheme per month — the ideal shape
for ``GenericHoldingsAdapter`` + the shared ``parse_sebi_excel`` (sheet 0
carries the equity table with a standard ISIN / NAME / % to NAV header at
row 8).

Discovery is the only bespoke part. The statutory-disclosures page
(https://quantmutual.com/statutory-disclosures) is a classic ASP.NET
WebForms SPA: the markup ships no portfolio .xlsx links; instead a jQuery
``$.ajax`` POST hits a server PageMethod that returns an HTML ``<ul>``
fragment of per-scheme links. There are two relevant categories:

  - cat "MONTHLY PORTFOLIO"            → ONE consolidated multi-sheet workbook
                                          per month (inconsistent filenames).
  - cat "MONTHLY PORTFOLIO - FUND - WISE" → ONE clean Excel PER SCHEME, with
                                          the printed scheme name as link text.

We use the FUND-WISE path: it is per-scheme (so the generic single-scheme
parser applies unchanged) and the link text gives us the exact printed
scheme name. The endpoint is a two-step PageMethod:

  POST /statutorydisclosures.aspx/displaydisclouser1
    body {id:'<YYYY>',cat:'MONTHLY PORTFOLIO - FUND - WISE'}
    → <li id='<month#>' ...>Jan</li> ... (just the month tabs for that year)

  POST /statutorydisclosures.aspx/displaydisclouser2
    body {id:'<month#>',cat:'MONTHLY PORTFOLIO - FUND - WISE',tab:'<YYYY>'}
    → <ul><li><a href='/Admin/disclouser/quant_<Scheme>_<Mon>_<YYYY>.xlsx'
         target='_blank'>quant <Scheme></a></li> ...

``<month#>`` is the calendar month number (1=Jan … 12=Dec), so we can call
``displaydisclouser2`` directly without scraping the month tabs first. The
returned hrefs are root-relative under ``/Admin/disclouser/`` and absolutize
against the quantmutual.com origin.

The PageMethod response is JSON ``{"d": "<html fragment>"}`` with HTML
entities escaped; we json-decode then html-unescape before regexing the
anchors. The data month maps to itself (the April-2026 fund-wise file is
keyed by month=4, year=2026 — no publish-month offset).

Excel layout (validated against quant Flexi Cap Fund, April 2026):
- Single sheet named after the scheme (e.g. 'quant_Flexi_Cap_Fund').
- Rows 0-3: scheme banner + "MONTHLY PORTFOLIO STATEMENT…".
- Row 7: column header — SR | ISIN | NAME OF THE INSTRUMENT | RATING |
  INDUSTRY | QUANTITY | MARKET VALUE(Rs.in Lakhs) | % to NAV | YTM.
- Row 8+: section banners ('EQUITY & EQUITY RELATED', '(a) Listed / awaiting
  listing…') interleaved with ISIN-bearing holding rows; % to NAV is in
  percent units.

This is the canonical SEBI layout, so ``parse_sebi_excel`` (sheet_index=0)
auto-detects the header, ISIN/name/weight columns, and the percent unit —
no bespoke parse_excel needed.
"""

from __future__ import annotations

import html as _html
import json
import re

import httpx

from mfs.config import get_settings
from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_ORIGIN = "https://quantmutual.com"
_LISTING_API = f"{_ORIGIN}/statutorydisclosures.aspx/displaydisclouser2"
_FUNDWISE_CAT = "MONTHLY PORTFOLIO - FUND - WISE"

# Anchor in the returned <ul> fragment: href to a per-scheme Excel under
# /Admin/disclouser/, with the printed scheme name as the link text. We keep
# the href tight (no quote/space) and take the link text as the scheme name.
_ANCHOR_RE = re.compile(
    r"<a\s+href='(?P<url>/Admin/disclouser/[^']+?\.xlsx)'\s+"
    r"target='_blank'>(?P<label>[^<]+)</a>",
    re.IGNORECASE,
)


def _data_month_parts(ym: str) -> tuple[int, str]:
    """data_ym='2026-04' → (4, '2026'). quant keys the fund-wise listing by
    calendar month number + year, with NO publish-month offset (the
    April-2026 data file is served under month=4, year=2026)."""
    y, m = map(int, ym.split("-"))
    return m, f"{y:04d}"


@register_adapter
class QuantHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "quant"
    source_label = "quant Mutual Fund"
    # Per-scheme files put the portfolio on sheet 0 in standard SEBI layout.
    sheet_index = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """POST the fund-wise PageMethod for the data month and parse the
        returned anchor list into {printed_scheme_name: absolute_excel_url}."""
        month, year = _data_month_parts(ym)
        body = json.dumps(
            {"id": str(month), "cat": _FUNDWISE_CAT, "tab": year}
        ).encode("utf-8")
        s = get_settings()
        with httpx.Client(
            timeout=s.http_timeout,
            headers={
                "User-Agent": s.user_agent,
                "Content-Type": "application/json; charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": f"{_ORIGIN}/statutory-disclosures",
            },
            follow_redirects=True,
        ) as c:
            r = c.post(_LISTING_API, content=body)
            r.raise_for_status()
            # ASP.NET PageMethod wraps the HTML fragment in {"d": "..."}.
            fragment = _html.unescape(json.loads(r.text).get("d") or "")

        out: dict[str, str] = {}
        for m in _ANCHOR_RE.finditer(fragment):
            url = m.group("url")
            if url.startswith("/"):
                url = _ORIGIN + url
            scheme = re.sub(r"\s+", " ", _html.unescape(m.group("label"))).strip()
            if not scheme:
                continue
            # First occurrence wins on the rare duplicate.
            out.setdefault(scheme, url)
        log.info(
            "holdings.quant.discover",
            ym=ym, month=month, year=year, n_schemes=len(out),
        )
        return out
