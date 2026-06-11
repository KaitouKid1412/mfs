"""Baroda BNP Paribas Mutual Fund monthly portfolio holdings adapter (Phase 5).

Baroda BNP Paribas publishes ONE SEBI-format ``.xlsx`` per scheme per month —
the ideal shape for ``GenericHoldingsAdapter`` + the shared
``parse_sebi_excel`` (sheet 0 carries the equity table with the standard
ISIN / NAME / % to Net Assets header at row 3).

Discovery (CodeIgniter SPA + paginated AJAX listing)
----------------------------------------------------
The "Monthly Portfolio of Scheme" page
(https://www.barodabnpparibasmf.in/downloads/monthly-portfolio-scheme) is a
CodeIgniter-rendered list whose first page ships only ~6 ``<li>`` cards in
the static HTML; the rest load via a "LOAD MORE" button backed by an AJAX
PageMethod (see /assets/ajax/load_more_documents.js):

    POST https://www.barodabnpparibasmf.in/ajax-load-more-documents
    form body:
        csrf_test_name = <token>   (double-submit: also set as a cookie)
        cnt            = <total>   (the page's #total_cnt hidden input)
        pagination     = <page#>   (1, then the server-returned next value)
        send_category  = 17        (the monthly-portfolio category id)
        send_year      = <YYYY>    (the data-month year tab; e.g. 2026)
        remaining_cnt  = 0
    → JSON {"data": "<li>…</li>…", "pagination": <next>, "status": "Y"|"N",
            "total_row": …, "remaining_cnt": …}

Each ``<li>`` card carries a printed title
    "Monthly Portfolio for <Scheme> as on 30th April 2026"
and an absolute ``.xlsx`` link under /assets/download_documents/. We page
until ``status == "N"`` (feeding the server-returned ``pagination`` back),
accumulate every card, and keep only those whose title ends with the data
month's "<DD>th <Month> <YYYY>" — the portfolio "as on" the month-end.

The CSRF token is a CodeIgniter double-submit token: it must be sent BOTH as
the ``csrf_test_name`` form field AND as the matching cookie, so we drive the
whole flow inside one ``httpx.Client`` that first GETs the page (to seed the
cookie + read the token) and then POSTs the listing endpoint.

The category id (17) and year tab are the only month-varying inputs, so next
month works unchanged: pass ``send_year`` for the requested ym and filter on
that ym's month-end title. The publish date in the card (uploadDate) is the
following month, but the title's "as on" date IS the data-month-end — we key
off the title, not the upload date.

Excel layout (validated against Baroda BNP Paribas Flexi Cap Fund, Apr 2026)
---------------------------------------------------------------------------
- Single sheet, named after an internal scheme code (e.g. 'T0ME25').
- Row 0: scheme-name banner.
- Row 2: "Monthly Portfolio Statement as on April 30, 2026".
- Row 3: header — col B='Name of the Instrument', col C='ISIN',
  col D='Industry / Rating', col E='Quantity',
  col F='Market/Fair Value (Rs. in Lakhs)', col G='% to Net Assets',
  col H='YTM~', col I='YTC^'.
- Row 4+: section banners ('Equity & Equity related',
  '(a) Listed / awaiting listing on Stock Exchanges') interleaved with
  ISIN-bearing holding rows.
- CRITICAL UNIT NOTE: like Nippon/Sundaram, '% to Net Assets' is stored as a
  **fraction** (0.0434 == 4.34%), not a percentage.

This is the canonical SEBI layout, so ``parse_sebi_excel`` (sheet_index=0)
auto-detects the header row, the ISIN/name/weight columns, and the
fraction-vs-percent weight unit — no bespoke ``parse_excel`` needed.
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

_ORIGIN = "https://www.barodabnpparibasmf.in"
_LISTING_PAGE = f"{_ORIGIN}/downloads/monthly-portfolio-scheme"
_LISTING_API = f"{_ORIGIN}/ajax-load-more-documents"

# The CodeIgniter category id for "Monthly Portfolio of Scheme" (read off the
# page's #category hidden input). Stable across months.
_CATEGORY_ID = "17"

# Hard cap on AJAX pages so a server-side pagination bug can't loop forever.
# ~30 cards/page * 40 pages comfortably covers a full year of ~180 docs.
_MAX_PAGES = 60

_MONTH_FULL = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# One card: printed title in <p class="file-name"> followed (within the same
# <li>) by the scheme's .xlsx under /assets/download_documents/. We capture
# the first such .xlsx after each title; the duplicate share-links that follow
# point at the same file and are ignored by setdefault.
_CARD_RE = re.compile(
    r'<p\s+class="file-name">(?P<title>.*?)</p>.*?'
    r'href="(?P<url>https://www\.barodabnpparibasmf\.in/'
    r'assets/download_documents/[^"]+?\.xls[x]?)"',
    re.IGNORECASE | re.DOTALL,
)

_CSRF_RE = re.compile(
    r'name="csrf_test_name"\s+value="(?P<tok>[^"]+)"', re.IGNORECASE
)
_TOTAL_RE = re.compile(
    r'id="total_cnt"\s+value="(?P<n>\d+)"', re.IGNORECASE
)


def _month_end_suffix(ym: str) -> str:
    """data_ym='2026-04' -> '30th April 2026' (the token the card title ends
    with for the portfolio "as on" the data-month-end).

    Day-of-month + the ordinal suffix are derived from the calendar so Feb
    (28th/29th) and 31-day months resolve correctly next month too."""
    import calendar

    y, m = map(int, ym.split("-"))
    day = calendar.monthrange(y, m)[1]
    suffix = (
        "th"
        if 10 <= day % 100 <= 20
        else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    )
    return f"{day}{suffix} {_MONTH_FULL[m - 1]} {y:04d}"


def _scheme_from_title(title: str) -> str | None:
    """Strip the 'Monthly Portfolio for ' prefix and ' as on <date>' suffix
    from a card title to recover the printed scheme name.

    'Monthly Portfolio for Baroda BNP Paribas Flexi Cap Fund as on 30th
    April 2026' -> 'Baroda BNP Paribas Flexi Cap Fund'. Returns None if the
    title doesn't match the expected shape."""
    m = re.match(
        r"^\s*Monthly Portfolio for\s+(?P<name>.+?)\s+as on\s+.+$",
        title,
        re.IGNORECASE,
    )
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group("name")).strip()


@register_adapter
class BarodaBnpHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "baroda_bnp"
    source_label = "Baroda BNP Paribas Mutual Fund"
    # Per-scheme files put the portfolio on sheet 0 in standard SEBI layout
    # with fraction weight units — parse_sebi_excel handles both.
    sheet_index = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Page the monthly-portfolio AJAX listing for the data month's year,
        and return {printed_scheme_name: absolute_excel_url} for cards whose
        title is "as on" the data-month-end."""
        year, _ = ym.split("-")
        want_suffix = _month_end_suffix(ym).lower()
        s = get_settings()

        out: dict[str, str] = {}
        with httpx.Client(
            timeout=s.http_timeout,
            headers={"User-Agent": s.user_agent},
            follow_redirects=True,
        ) as c:
            # Seed the CSRF cookie + read the matching token and total count.
            page = c.get(_LISTING_PAGE)
            page.raise_for_status()
            html = page.text
            tok_m = _CSRF_RE.search(html)
            if not tok_m:
                log.error("holdings.baroda_bnp.no_csrf_token", ym=ym)
                return {}
            token = tok_m.group("tok")
            total_m = _TOTAL_RE.search(html)
            total = total_m.group("n") if total_m else "0"

            # Page 1's cards are already in the static HTML; the AJAX endpoint
            # re-serves them paginated, so we drive everything through AJAX for
            # a single, uniform parse path.
            page_no = 1
            for _ in range(_MAX_PAGES):
                body = {
                    "csrf_test_name": token,
                    "cnt": total,
                    "pagination": str(page_no),
                    "send_category": _CATEGORY_ID,
                    "send_year": year,
                    "remaining_cnt": "0",
                }
                r = c.post(
                    _LISTING_API,
                    data=body,
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": _LISTING_PAGE,
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                    },
                )
                r.raise_for_status()
                try:
                    payload = r.json()
                except json.JSONDecodeError:
                    log.error(
                        "holdings.baroda_bnp.bad_json",
                        ym=ym, page=page_no, head=r.text[:200],
                    )
                    break

                fragment = payload.get("data") or ""
                for cm in _CARD_RE.finditer(fragment):
                    # Card titles are DOUBLE HTML-escaped in the AJAX JSON
                    # ('&amp;amp;' for '&'), so unescape twice to recover the
                    # exact printed scheme name (e.g. 'Banking & Financial
                    # Services Fund') — the name must match scheme_master for
                    # slug→amc_code resolution.
                    title = re.sub(
                        r"\s+",
                        " ",
                        _html.unescape(_html.unescape(cm.group("title"))),
                    ).strip()
                    if not title.lower().endswith(want_suffix):
                        continue
                    scheme = _scheme_from_title(title)
                    if not scheme:
                        continue
                    out.setdefault(scheme, cm.group("url"))

                if str(payload.get("status", "")).upper() == "N":
                    break
                nxt = payload.get("pagination")
                try:
                    nxt_i = int(nxt)
                except (TypeError, ValueError):
                    break
                if nxt_i <= page_no:
                    break
                page_no = nxt_i

        log.info(
            "holdings.baroda_bnp.discover",
            ym=ym, year=year, n_schemes=len(out),
        )
        return out
