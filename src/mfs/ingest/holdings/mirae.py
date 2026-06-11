"""Mirae Asset Mutual Fund monthly portfolio holdings adapter.

Mirae publishes one SEBI-format Excel per scheme per month. The downloads
page (https://www.miraeassetmf.co.in/downloads/portfolio) is a jQuery SPA:
the static HTML carries NO .xlsx links. The portfolio list is rendered
client-side from an ASP.NET AJAX WebService call we reverse-engineered out
of the page's `/DownloadPortfolio.js` + `/main.js` bundles:

    POST https://www.miraeassetmf.co.in/AjaxService/GetDownloadsData
    Content-Type: application/json;charset=utf-8
    body: {"request":{"modulename":"portfolio_tab1","pgno":<n>,"pgsize":<k>}}

(`portfolio_tab1` is the "Monthly Portfolio" tab; `portfolio_tab2` is the
half-yearly tab and `portfolio_tab3` the fortnightly tab — we want tab1.)

The JSON response is shaped:
    {"Data":[{"Title":"Portfolio Details as on 30th April 2026 for
              Mirae Asset Flexi Cap Fund",
              "URL":"/docs/default-source/portfolios/mafcf-april2026.xlsx",
              "PublishDate":"/Date(1778198400000)/", ...}, ...],
     "DataCount": 3333, "ReturnCode": "0", ...}

Records are returned newest-first (most recent data month at the top), so
the current month's entries lead the first page. We page through with a
generous page size, parse each Title for its "<Month> <Year>" data month,
and keep only the rows that match the requested `ym`. The scheme name is the
text after "for " in the Title. The URL filename's date separator varies by
month (April uses 'mafcf-april2026.xlsx', March uses 'milpf_march2026.xlsx')
— irrelevant to us because we take the URL verbatim from the JSON rather
than constructing it.

Excel layout (validated against Mirae Asset Flexi Cap Fund, April 2026):
- Portfolio table is on sheet 0 in the standard SEBI layout (ISIN / Name of
  the Instrument / Industry / Quantity / Market Value / % to NAV columns,
  weights in percent). The shared `parse_sebi_excel` auto-detects the header
  row, column positions, and weight unit — so we inherit `parse_excel` from
  GenericHoldingsAdapter unchanged. Flexi Cap yields 92 equity ISIN rows
  summing to ~98%.
"""

from __future__ import annotations

import re

import httpx

from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_LISTING_API = "https://www.miraeassetmf.co.in/AjaxService/GetDownloadsData"
_HOST = "https://www.miraeassetmf.co.in"

# The module key for the monthly-portfolio tab (tab2 = half-yearly,
# tab3 = fortnightly).
_MODULE_NAME = "portfolio_tab1"

# Pull more than one month's worth per page; the live count for a single
# data month is ~90 schemes and records are newest-first, so one page of
# 250 comfortably covers the current month. We still paginate defensively.
_PAGE_SIZE = 250
_MAX_PAGES = 20

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# Disambiguation for discovered names that are an exact token SUBSET of a
# sibling scheme's name. The disclosure prints "Mirae Asset Midcap Fund"
# whose canonical form (MIRAE ASSET MIDCAP FUND) is a strict subset of
# "Mirae Asset Large & Midcap Fund" (MIRAE ASSET LARGE MIDCAP FUND). Both
# score token_set_ratio=100 against the bare printed name, and the shared
# matcher's Levenshtein tie-break then picks the WRONG sibling: scheme_master
# stores the true Midcap row as "Mirae Asset Midcap Fund- Direct Growth
# Option" — its plan/option suffix uses "Direct Growth Option" (not "Direct
# Plan ..."), which the shared canonicalizer does NOT strip, leaving the
# canonical key as "...MIDCAP FUND DIRECT GROWTH OPTION" (43 chars). The
# bare printed name is edit-distance-closer to the 28-char "...LARGE MIDCAP
# FUND" key, so the tie-break mis-routes the Midcap holdings onto Large &
# Midcap. We can't touch the shared matcher or scheme_master, so we emit the
# printed name in the exact (separator-less) form that canonicalizes to the
# true Midcap row's key, making the match unambiguous (score 100, exact key).
_DISAMBIGUATE_PRINTED_NAME = {
    "Mirae Asset Midcap Fund": "Mirae Asset Midcap Fund Direct Growth Option",
}

# "Portfolio Details as on 30th April 2026 for Mirae Asset Flexi Cap Fund"
# -> month='April', year='2026', scheme='Mirae Asset Flexi Cap Fund'.
_TITLE_RE = re.compile(
    r"^Portfolio Details as on\s+\d{1,2}(?:st|nd|rd|th)?\s+"
    r"(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})\s+for\s+(?P<scheme>.+?)\s*$",
    re.IGNORECASE,
)


def _target_month_year(ym: str) -> tuple[str, str]:
    """data_ym='2026-04' -> ('April', '2026'). The disclosure Title keys the
    portfolio by its DATA month-end, which is what we filter on."""
    y, m = map(int, ym.split("-"))
    return _MONTHS[m - 1], f"{y:04d}"


def _request_page(client: httpx.Client, pgno: int) -> dict:
    body = (
        '{"request":{"modulename":"' + _MODULE_NAME
        + '","pgno":' + str(pgno) + ',"pgsize":' + str(_PAGE_SIZE) + "}}"
    ).encode("utf-8")
    r = client.post(_LISTING_API, content=body)
    r.raise_for_status()
    return r.json()


@register_adapter
class MiraeHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "mirae"
    source_label = "Mirae Asset Mutual Fund"

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {printed_scheme_name: absolute_excel_url} for data month ``ym``.

        Pages through the AJAX listing newest-first, keeping only rows whose
        Title resolves to the requested data month. Because the listing is
        date-descending, once we've seen the target month and then a page
        yields none of it (older months only), we stop early.
        """
        want_month, want_year = _target_month_year(ym)
        want_month = want_month.lower()

        out: dict[str, str] = {}
        seen_target = False
        with httpx.Client(
            timeout=60.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 "
                    "Safari/537.36"
                ),
                "Content-Type": "application/json;charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": f"{_HOST}/downloads/portfolio",
            },
            follow_redirects=True,
        ) as client:
            for pgno in range(1, _MAX_PAGES + 1):
                payload = _request_page(client, pgno)
                if str(payload.get("ReturnCode")) not in ("0", "1"):
                    log.warning(
                        "holdings.mirae.bad_return_code",
                        ym=ym, pgno=pgno,
                        code=payload.get("ReturnCode"),
                        msg=payload.get("ReturnMsg"),
                    )
                    break
                rows = payload.get("Data") or []
                if not rows:
                    break

                page_hit = False
                for row in rows:
                    title = (row.get("Title") or "").strip()
                    url = (row.get("URL") or "").strip()
                    if not title or not url:
                        continue
                    mt = _TITLE_RE.match(re.sub(r"\s+", " ", title))
                    if not mt:
                        continue
                    if (
                        mt.group("month").lower() != want_month
                        or mt.group("year") != want_year
                    ):
                        continue
                    if not url.lower().endswith((".xlsx", ".xls")):
                        # Some legacy months expose PDFs only — skip those;
                        # the generic Excel parser needs an ISIN-bearing sheet.
                        continue
                    page_hit = True
                    seen_target = True
                    scheme = re.sub(r"\s+", " ", mt.group("scheme")).strip()
                    scheme = _DISAMBIGUATE_PRINTED_NAME.get(scheme, scheme)
                    abs_url = url if url.startswith("http") else _HOST + url
                    out.setdefault(scheme, abs_url)

                # Listing is newest-first. Once we've collected the target
                # month and a subsequent page has none of it, we've moved past
                # it into older months — stop.
                if seen_target and not page_hit:
                    break

        log.info("holdings.mirae.discover", ym=ym, n_schemes=len(out))
        return out
