"""Navi Mutual Fund monthly portfolio holdings adapter (Phase 5).

Navi publishes ONE SEBI-format Excel per scheme per month — the ideal shape
for ``GenericHoldingsAdapter`` + the shared ``parse_sebi_excel``.

The disclosures page (https://navi.com/mutual-fund/downloads/portfolio) is a
WordPress (Elementor) SPA whose static HTML carries NO ``.xlsx`` links — the
portfolio listing is loaded by AJAX. So this is the SBI discovery shape
(JSON listing API), not the HDFC links-in-HTML shape.

Backing API (reverse-engineered from the theme bundle
``wp-content/themes/hello-theme-child/assets/js/app.js``):

    POST https://navi.com/wp-json/nv/v1/documents
    headers: WP-NONCE: <nonce from navi_property on the page>
    form body:
      financial_year = "<FYstart>-<FYend>"   (Indian FY, Apr–Mar; April 2026
                                               data → "2026-2027")
      value          = "<MonthName>"          (the data month, e.g. "April")
      category       = "884"                  (the Monthly-portfolio category;
                                               884 = Monthly, 885 Fortnightly,
                                               886 HalfYearly, 887 Quarterly)
      type           = "Monthly"
      order          = "DESC"

Response: ``{"success":true,"data":[{"title":..,"url":..}]}`` where ``url`` is
an absolute ``.xlsx`` on ``public-assets.prod.navi-tech.in`` and ``title`` is
``"<Scheme Name> 1st – 30th April 2026"`` (HTML-entity encoded: ``&amp;``,
``&#8211;``). We strip the trailing ``"<DD><ord> – <DD><ord> <Month> <Year>"``
date-range to recover the printed scheme name.

NONCE NOTE: the endpoint only checks that a ``WP-NONCE`` header is PRESENT —
it does not currently validate the value (a wrong nonce still returns 200; a
missing header returns 403 "nonce missing"). We nonetheless scrape the live
nonce from the page's inline ``navi_property`` blob on every run so the
adapter keeps working if Navi tightens validation later; if scraping fails we
fall back to a placeholder header so the present-check still passes.

URL convention (verified for April 2026):
    https://public-assets.prod.navi-tech.in/navi-website-assests/documents/
        Navi_<Scheme_With_Underscores>_1st_30th_<Month>_<Year>_<uploadts>.xlsx

The upload timestamp suffix is opaque and varies per file, so we DON'T derive
URLs by template — we take the absolute URL straight from the API response.

Excel layout (validated against Navi Flexi Cap Fund, April 2026 — 67 equity
ISIN rows summing to 91.8% to NAV):
- Single sheet ('Sheet1').
- Rows 4-6: "NAVI MUTUAL FUND" / "<Scheme>" / "Monthly Portfolio Statement".
- Row 7: header — Name of the Instrument | ISIN | Industry/Rating | Quantity |
  Market/Fair Value (Rs. in Lacs) | % to Net Assets | YIELD.
- Row 8+: 'Equity & Equity related' / '(a) Listed / awaiting listing' section
  banners interleaved with ISIN-bearing holding rows; '% to Net Assets' is in
  percent units.

This is the canonical SEBI layout, so ``parse_sebi_excel`` (sheet_index=0)
auto-detects the header, ISIN/name/weight columns, and the percent unit — no
bespoke parse_excel needed.
"""

from __future__ import annotations

import html as _html
import re
from pathlib import Path

import httpx

from mfs import paths
from mfs.config import get_settings
from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import download_to, fetch_bytes
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = "https://navi.com/mutual-fund/downloads/portfolio"
_DOCUMENTS_API = "https://navi.com/wp-json/nv/v1/documents"
# The Monthly-portfolio document category id on the WordPress backend (read off
# the page's `data-category` attribute for the monthly dropdown container).
_MONTHLY_CATEGORY = "884"

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Scrape the live REST nonce from the inline `navi_property` config blob.
_NONCE_RE = re.compile(r'"nonce"\s*:\s*"([0-9a-f]+)"', re.IGNORECASE)

# Trailing "1st – 30th April 2026" date range that suffixes every API title.
# Tolerant of the en-dash/em-dash/hyphen Navi uses between the two days.
_TITLE_DATE_SUFFIX_RE = re.compile(
    r"\s+\d{1,2}(?:st|nd|rd|th)\s*[–—\-]\s*"
    r"\d{1,2}(?:st|nd|rd|th)\s+[A-Za-z]+\s+\d{4}\s*$",
    re.IGNORECASE,
)


def _financial_year(ym: str) -> str:
    """Indian financial year string for data month ``ym`` (YYYY-MM).

    FY runs April–March: months Apr..Dec belong to FY <year>-<year+1>; months
    Jan..Mar belong to FY <year-1>-<year>. April 2026 → '2026-2027'.
    """
    y, m = map(int, ym.split("-"))
    start = y if m >= 4 else y - 1
    return f"{start:04d}-{start + 1:04d}"


def _month_name(ym: str) -> str:
    """'2026-04' → 'April' (the `value` the documents API expects)."""
    _, m = map(int, ym.split("-"))
    return _MONTH_NAMES[m - 1]


def _scheme_from_title(title: str) -> str | None:
    """Recover the printed scheme name from an API ``title``.

    Decodes HTML entities and strips the trailing date-range, e.g.
    'Navi Large &amp; Midcap Fund 1st &#8211; 30th April 2026'
        -> 'Navi Large & Midcap Fund'.
    """
    if not title:
        return None
    text = _html.unescape(title)
    name = _TITLE_DATE_SUFFIX_RE.sub("", text)
    name = re.sub(r"\s+", " ", name).strip()
    return name or None


def _scrape_nonce() -> str:
    """Read the live REST nonce off the disclosures page's inline config.

    The endpoint only checks the header's PRESENCE today, but we use the real
    nonce for forward-safety. Falls back to a placeholder if the page layout
    changes — a present-but-wrong nonce still satisfies the current check.
    """
    try:
        html = fetch_bytes(_DISCLOSURE_PAGE).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 — discovery degrades, doesn't crash
        log.warning("holdings.navi.nonce_page_fetch_failed", err=str(e))
        return "0"
    m = _NONCE_RE.search(html)
    if not m:
        log.warning("holdings.navi.nonce_not_found")
        return "0"
    return m.group(1)


@register_adapter
class NaviHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "navi"
    source_label = "Navi Mutual Fund"
    # Per-scheme files put the portfolio on sheet 0 in standard SEBI layout.
    sheet_index = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Query the documents API for the requested data month and return
        {printed_scheme_name: absolute_xlsx_url}."""
        nonce = _scrape_nonce()
        body = {
            "financial_year": _financial_year(ym),
            "value": _month_name(ym),
            "category": _MONTHLY_CATEGORY,
            "type": "Monthly",
            "order": "DESC",
        }
        settings = get_settings()
        with httpx.Client(
            timeout=settings.http_timeout,
            headers={
                "User-Agent": settings.user_agent,
                "WP-NONCE": nonce,
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            follow_redirects=True,
        ) as c:
            r = c.post(_DOCUMENTS_API, data=body)
            r.raise_for_status()
            payload = r.json()

        if not isinstance(payload, dict) or not payload.get("success"):
            log.warning("holdings.navi.api_unsuccessful", ym=ym, payload=str(payload)[:200])
            return {}

        out: dict[str, str] = {}
        for doc in payload.get("data") or []:
            url = doc.get("url")
            scheme = _scheme_from_title(doc.get("title", ""))
            if not url or not scheme:
                continue
            if not url.lower().endswith((".xlsx", ".xls")):
                # Non-Excel (e.g. PDF) disclosure — skip; we need ISINs.
                continue
            out.setdefault(scheme, url)

        log.info("holdings.navi.discover", ym=ym, n_schemes=len(out))
        return out

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        out = paths.holdings_excel_raw(self.amc_slug, ym, scheme_filename)
        if out.exists():
            return out
        return download_to(url, out)
