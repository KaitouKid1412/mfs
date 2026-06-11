"""WhiteOak Capital Mutual Fund monthly portfolio holdings adapter (Phase 5).

WhiteOak publishes one SEBI-format Excel per scheme per month. The
disclosures page (``mf.whiteoakamc.com/regulatory-disclosures/scheme-
portfolios``) is a Next.js SPA — the static HTML carries no ``.xlsx`` links
— but its document table is populated client-side from a Strapi CMS
collection API. The SPA's data layer (recovered from the page's JS bundle,
``getDisclosureSchemePortfolioDataFiltered``) is:

    GET https://cms.whiteoakamc.com/api/scheme-portfolios
        ?filters[period][$eq]=Monthly
        &pagination[pageSize]=...&populate=*

Each entry's ``attributes`` carries:
- ``scheme_name``    e.g. "WhiteOak Capital Flexi Cap Fund"
- ``period``         "Monthly" (we only want the monthly portfolio)
- ``doc_name``       e.g. "WhiteOak Capital Flexi Cap Fund Monthly Portfolio
                     Disclosure - 30th April2026" — carries the DATA month.
- ``published_date`` the PUBLISH date (NOT the data month): the
                     "as on 30-Apr-2026" snapshot is published 2026-05-08;
                     the "as on 31-Mar-2026" snapshot 2026-04-07. So we must
                     NOT filter on ``published_date`` — the data month lives
                     in ``doc_name``.
- ``doc_file.data.attributes.url`` — the absolute download URL on
                     ``content.whiteoakamc.com`` (a true ``.xlsx``).

DISCOVERY: page the ``period=Monthly`` collection, and for each entry parse
the DATA month out of ``doc_name`` (a ``<MonthName><opt-space><YYYY>`` token
such as "April2026" / "April 2026"); keep the entries whose data month
equals the requested ``ym``. This is parameterized by ``ym`` so next month
works unchanged.

Per-scheme Excel layout (validated against Flexi Cap, April 2026):
- Single sheet (named after an internal scheme code, e.g. 'YM004').
- A clean generic SEBI portfolio table: header row with ISIN / Name of the
  Instrument / Industry / Quantity / Market Value / % to NAV, section
  banners ("Equity & Equity related", "(a) Listed / awaiting listing ...",
  etc.) and ``% to NAV`` printed as a true percent. Because the layout is
  generic-SEBI, ``fetch_excel`` + ``parse_excel`` are inherited from
  ``GenericHoldingsAdapter`` — the shared ``parse_sebi_excel`` auto-detects
  the header, columns, and percent-vs-fraction unit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from mfs import paths
from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_API = "https://cms.whiteoakamc.com/api/scheme-portfolios"

_MONTHS_FULL = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

# A "<MonthName><optional whitespace><4-digit-year>" token inside doc_name.
# WhiteOak prints it as "April2026" (no space) but we tolerate a space in
# case their web team ever adds one.
_DOC_MONTH_RE = re.compile(
    r"(?P<month>january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s*(?P<year>\d{4})",
    re.IGNORECASE,
)

# The SPA hits the CMS with a normal browser fingerprint; the bare API is
# public but 403s some non-browser UAs, so we mirror the SPA's headers.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://mf.whiteoakamc.com/",
    "Origin": "https://mf.whiteoakamc.com",
}


def _doc_name_ym(doc_name: str) -> str | None:
    """Reverse-derive data_ym (YYYY-MM) from a doc_name's month token.

    Returns None if no recognizable ``<Month><Year>`` token is present.
    """
    if not doc_name:
        return None
    m = _DOC_MONTH_RE.search(doc_name)
    if not m:
        return None
    month = _MONTHS_FULL.index(m.group("month").lower()) + 1
    return f"{int(m.group('year')):04d}-{month:02d}"


@register_adapter
class WhiteoakCapitalHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "whiteoak_capital"
    source_label = "WhiteOak Capital Mutual Fund"

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Page the Strapi ``scheme-portfolios`` collection and return
        {printed_scheme_name: absolute_excel_url} for data month ``ym``.

        The data month is matched against the month token in each entry's
        ``doc_name`` (NOT ``published_date``, which is the publish month).
        """
        out: dict[str, str] = {}
        page = 1
        page_count = 1
        with httpx.Client(
            timeout=60.0, headers=_HEADERS, follow_redirects=True
        ) as c:
            while page <= page_count:
                params = {
                    "filters[period][$eq]": "Monthly",
                    "pagination[page]": str(page),
                    "pagination[pageSize]": "100",
                    "sort[0]": "published_date:desc",
                    "populate": "*",
                }
                r = c.get(_API, params=params)
                r.raise_for_status()
                payload = json.loads(r.text)
                page_count = (
                    (payload.get("meta", {}) or {})
                    .get("pagination", {})
                    .get("pageCount", 1)
                )
                for entry in payload.get("data", []) or []:
                    a = entry.get("attributes", {}) or {}
                    if _doc_name_ym(a.get("doc_name") or "") != ym:
                        continue
                    df = (a.get("doc_file") or {}).get("data")
                    if not df:
                        continue
                    fa = df.get("attributes", {}) or {}
                    url = fa.get("url")
                    ext = (fa.get("ext") or "").lower()
                    if not url or ext not in (".xlsx", ".xls"):
                        # Portfolios are Excel; skip any stray PDF/empty rows.
                        continue
                    scheme = re.sub(
                        r"\s+", " ", str(a.get("scheme_name") or "")
                    ).strip()
                    if scheme:
                        out.setdefault(scheme, url)
                page += 1
        log.info(
            "holdings.whiteoak_capital.discover",
            ym=ym, n_schemes=len(out),
        )
        return out

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Download (cached) a scheme's Excel from ``content.whiteoakamc.com``.

        We override the inherited fetch because the CMS asset host 403s the
        default pipeline User-Agent; a browser fingerprint returns 200.
        """
        out = paths.holdings_excel_raw(self.amc_slug, ym, scheme_filename)
        if out.exists():
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(
            timeout=120.0, headers=_HEADERS, follow_redirects=True
        ) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.content
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(out)
        return out
