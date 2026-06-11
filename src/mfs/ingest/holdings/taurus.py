"""Taurus Mutual Fund monthly portfolio holdings adapter (Phase 5).

Taurus publishes ONE SEBI-format ``.xlsx`` per scheme per month — the ideal
shape for ``GenericHoldingsAdapter`` + the shared ``parse_sebi_excel``.

Discovery (Drupal Views AJAX)
-----------------------------
The "Monthly Portfolio" page (https://www.taurusmutualfund.com/monthly-portfolio)
is a Drupal site whose portfolio listing is a Drupal **View**
(``monthly_portfolio`` / ``page_1``) with two exposed filters — Year and Month
— rendered through ``/views/ajax``. The statically-served page ships the filter
form (the ``<select>`` option ids below) but NO rows: rows render only after the
exposed filter POSTs to ``/views/ajax``. So discovery POSTs that endpoint with
the data month's Year+Month term ids and parses the returned Drupal-AJAX command
array.

Exposed-filter term ids (taxonomy term ids on the live site):
- Year:  ``field_monthly_portfolio_target_id`` — 2026→567, 2025→558, 2024→514,
  2023→473, 2022→456, 2021→427, 2020→418, 2019→293, 2018→57. The id is NOT a
  simple function of the year, so we maintain a small lookup keyed by calendar
  year (extend it as new years appear; an unmapped year raises).
- Month: ``field_month_target_id`` — January→281 … December→292 (contiguous, so
  computed as ``281 + (month - 1)``).

The view rows carry, per scheme, an ``<a>`` whose ``href`` is the scheme's
Excel under ``/sites/default/files/downloads/`` (note: the href has a stray
LEADING SPACE in the markup, which we strip) and a child ``<span>`` with the
clean printed scheme name. We pair href↔span per anchor.

URL pattern (verified for April-2026 and March-2026 data)::

    https://www.taurusmutualfund.com/sites/default/files/downloads/
      Taurus_<SchemeBlob>_Monthly_Portfolio_Report_Performance_<MonthName>_<YYYY>.xlsx

The filename encodes the DATA month-end directly using the full English month
name (April data → ``..._April_2026.xlsx``; Taurus publishes April data in May
but files it under the April term + April filename), so there is NO
publish-month offset — next month works by passing the new ``ym`` (the
May-2026 filter currently returns 0 rows, confirming data-month keying). We
also verify the filename's month/year matches ``ym`` before accepting a link,
defending against any stray cross-month row.

Excel layout (validated against Taurus Flexi Cap Fund, April 2026)
------------------------------------------------------------------
- Sheet 0 (named after the scheme code, e.g. 'TSS') holds the portfolio;
  sheet 1 ('… Performance') is a returns table with no SEBI header.
- Rows 0-4: AMC banner + "SCHEME NAME :" + "PORTFOLIO STATEMENT AS ON :".
- Row 5: header — col C='Name of the Instrument / Issuer', col D='ISIN',
  col E='Rating', col F='Industry ^', col G='Quantity',
  col H='Market value (Rs. in Lakhs)', col I='% to AUM', col J='Notes'.
- Row 7+: section banners ('EQUITY & EQUITY RELATED',
  'a) Listed/awaiting listing on Stock Exchanges', ...) interleaved with
  ISIN-bearing holding rows; '% to AUM' is stored as a PERCENT (7.96 == 7.96%).

This is the canonical SEBI layout, so ``parse_sebi_excel`` (sheet_index=0)
auto-detects the header row, the ISIN/name/weight columns, and the percent
weight unit — no bespoke ``parse_excel`` needed. ``fetch_excel`` is inherited
too (plain cached GET). Validated on April 2026: 8 schemes discovered (the 7
ranked equity funds + the Nifty 50 Index Fund); Taurus Flexi Cap Fund yields
~40 equity ISIN rows summing to ~95%.
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

_ORIGIN = "https://www.taurusmutualfund.com"
_DISCLOSURE_PAGE = f"{_ORIGIN}/monthly-portfolio"
_AJAX_ENDPOINT = f"{_ORIGIN}/views/ajax"

# Drupal View identity for the monthly-portfolio listing (from the page's
# drupal-settings-json ``views.ajaxViews`` block).
_VIEW_NAME = "monthly_portfolio"
_VIEW_DISPLAY_ID = "page_1"
_VIEW_DOM_ID = (
    "bb2049905de2e45c587e00e914776b37d8fef9587cea02acbe234f80fee3051d"
)

# Year exposed-filter term ids. The id is an arbitrary taxonomy term id, NOT a
# function of the year, so we maintain an explicit lookup. Extend as new years
# are published (read the new <option value=..> from the page's Year select).
_YEAR_TERM_ID: dict[int, str] = {
    2026: "567",
    2025: "558",
    2024: "514",
    2023: "473",
    2022: "456",
    2021: "427",
    2020: "418",
    2019: "293",
    2018: "57",
}

# Month exposed-filter term ids are contiguous: January=281 … December=292.
_MONTH_TERM_BASE = 281

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# A view row's download anchor: href to the scheme Excel under
# /sites/default/files/downloads/ (with a stray leading space in the markup)
# followed by an <img> icon and a <span> carrying the printed scheme name.
# Match the whole <a>…</a> so we pair the href with its span text.
_ANCHOR_RE = re.compile(
    r'<a\s+href="\s*(?P<url>/sites/default/files/downloads/'
    r'[^"]+?\.xlsx)"[^>]*>'
    r'.*?<span>(?P<label>[^<]+)</span>',
    re.IGNORECASE | re.DOTALL,
)

# Filename shape: Taurus_<blob>_Monthly_Portfolio_Report_Performance_<Mon>_<YYYY>.xlsx
_FILENAME_RE = re.compile(
    r"^Taurus_.+_Monthly_Portfolio_Report_Performance_"
    r"(?P<month>[A-Za-z]+)_(?P<year>\d{4})\.xlsx$",
    re.IGNORECASE,
)


def _year_term_id(year: int) -> str:
    tid = _YEAR_TERM_ID.get(year)
    if tid is None:
        raise ValueError(
            f"No Taurus Year exposed-filter term id for {year}; add it to "
            "_YEAR_TERM_ID from the monthly-portfolio page's Year <select>."
        )
    return tid


def _month_term_id(month: int) -> str:
    return str(_MONTH_TERM_BASE + (month - 1))


@register_adapter
class TaurusHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "taurus"
    source_label = "Taurus Mutual Fund"
    # Per-scheme files put the portfolio on sheet 0 in the standard SEBI
    # layout (percent weight units); parse_sebi_excel handles both. Sheet 1
    # is a performance table with no SEBI header, so sheet_index=0 is correct.
    sheet_index: int | None = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """POST the Drupal Views AJAX endpoint with the data month's Year +
        Month exposed-filter term ids and return {printed_scheme_name:
        absolute_excel_url} for ``ym`` (e.g. '2026-04')."""
        year, month = map(int, ym.split("-"))
        body = {
            "view_name": _VIEW_NAME,
            "view_display_id": _VIEW_DISPLAY_ID,
            "view_args": "",
            "view_path": "/monthly-portfolio",
            "view_base_path": "monthly-portfolio",
            "view_dom_id": _VIEW_DOM_ID,
            "pager_element": "0",
            "field_monthly_portfolio_target_id": _year_term_id(year),
            "field_month_target_id": _month_term_id(month),
            "_drupal_ajax": "1",
            "ajax_page_state[theme]": "taurus",
            "ajax_page_state[theme_token]": "",
        }
        s = get_settings()
        with httpx.Client(
            timeout=s.http_timeout,
            headers={
                "User-Agent": s.user_agent,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": _DISCLOSURE_PAGE,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            follow_redirects=True,
        ) as c:
            r = c.post(_AJAX_ENDPOINT, data=body)
            r.raise_for_status()
            cmds = json.loads(r.text)

        # Concatenate the HTML payloads of every Drupal-AJAX 'insert' command
        # (the rendered view rows live in the command that targets this view's
        # js-view-dom-id container).
        fragment = "\n".join(
            cmd.get("data") or ""
            for cmd in cmds
            if cmd.get("command") == "insert"
        )

        out: dict[str, str] = {}
        for m in _ANCHOR_RE.finditer(fragment):
            url = m.group("url").strip()
            filename = url.rsplit("/", 1)[-1]
            fm = _FILENAME_RE.match(filename)
            if not fm:
                continue
            # Verify the filename's month/year matches the requested data
            # month — defends against a stray cross-month row.
            fmonth = fm.group("month").lower()
            try:
                fmonth_idx = _MONTH_NAMES.index(fmonth.capitalize()) + 1
            except ValueError:
                continue
            if fmonth_idx != month or int(fm.group("year")) != year:
                continue
            scheme = re.sub(r"\s+", " ", _html.unescape(m.group("label"))).strip()
            if not scheme:
                continue
            out.setdefault(scheme, _ORIGIN + url)
        log.info(
            "holdings.taurus.discover",
            ym=ym, year=year, month=month, n_schemes=len(out),
        )
        return out
