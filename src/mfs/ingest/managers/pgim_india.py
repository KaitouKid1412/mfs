"""PGIM India Mutual Fund — factsheet adapter (Phase 3.D).

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/pgim_india/2026-04.pdf``, ~6.14 MB, 42 pages, 25
scheme detail pages — 1 large cap + 10 other equity/hybrid + 3 FoF + 8
debt + 1 index target-maturity preceded by cover / TOC / CEO note /
market review / SIP-and-disclosures back matter).

URL discovery
-------------
``www.pgimindiamf.com`` is an Angular SPA whose routes all redirect to
``www.pgimindia.com/mutual-funds`` (the parent AMC umbrella).  The factsheet
download page at ``/mutual-funds/forms-and-product-updates/Fund-Factsheet``
is rendered client-side from lazy-loaded webpack chunks; the chunks are
heavily obfuscated and the only API endpoint we could reach
(``/api/v1/brochure/get/file``) returns a stub success blob regardless of
query parameters, so we cannot resolve the per-month PDF URL via the SPA.

However, the AMC's CMS exposes the monthly PDFs under the stable path

    https://www.pgimindia.com/api/v1/brochure/about-us/image/
    Factsheet%20-%20{MonthName}%20{YYYY}.pdf

where ``{MonthName}`` is the **data month** in Title Case and
``{YYYY}`` is the data month's year.  Spaces are URL-encoded as ``%20``.
The URL pattern was confirmed by cross-referencing the third-party
aggregator ``advisorkhoj.com``'s PGIM-India download index (which links
every historical month from 2024 through 2026 using this exact pattern)
and HEAD-probing the April 2026 issue, which returned 200 with
``application/pdf``.  May 2026 (the next month) currently 204s, so the
publish lag is ~2-3 weeks past month-end — typical for the AMC.

``build_url(ym)`` encodes this pattern deterministically; no API
lookup is needed.

Layout findings driving the parser
----------------------------------
* Scheme detail pages span p9-p25 (equity / hybrid / FoF) and p28-p35
  (debt / target-maturity index).  Pages 26-27 are landscape-oriented
  "DEBT FUNDS RECKONER" comparison tables whose ``extract_text`` returns
  reversed glyph text (the PDF embeds them rotated 180°); they yield
  nothing under the title gate.  Pages 1-8 (cover / TOC / CEO / market
  review / equity outlook) and 36+ (TER table / SIP performance /
  glossary / disclosures) carry the AUM / PTR labels nowhere visible to
  the parser.

* Scheme title is rendered in a left-column logo band at the top of the
  page:
      Line 1 (y ~ 38-44, x ~ 35):  ``PGIM INDIA``
      Line 2 (y ~ 53-65, x ~ 35):  ``LARGE AND MID CAP FUND`` (all caps)
      Line 3 (y ~ 74-77, x ~ 35):  ``Large and Mid Cap Fund - An open
                                    ended equity scheme investing...``
                                    (Title Case description)
  Long titles wrap line 2 into line 3 (e.g. ``EMERGING MARKETS EQUITY``
  on row 1 + ``FUND OF FUND`` on row 2).  We collect all-uppercase
  tokens in the title band [y_pgim - 3, y_pgim + 40] at x0 < 230 in
  reading order, accepting ``&``/``-``/digits as part of the title, and
  stopping at the first FUND token (with a special case to allow
  ``OF FUND`` continuation for fund-of-fund schemes).  ``extract_text``
  column-bleeds the title with the right-side riskometer disclosure on
  every page so it isn't usable directly — word-position extraction is
  required.

* PTR is printed mid-page as the clean line
      ``Portfolio Turnover: 0.43``
  (or ``Portfolio Turnover: 0.23 (For Equity)`` on hybrid pages).  Value
  is a fraction (PGIM stores it as "times" already — 0.43 = 43%, no
  divide-by-100 needed).  Pages without a meaningful PTR (debt / liquid
  / overnight / FoF) simply omit the label.  Pages 18-20 (the three
  Fund-of-Fund schemes) carry no PTR.  The Arbitrage Fund page (p22)
  prints a high PTR (5.85x) which is real — arbitrage funds rotate
  derivatives near-daily.

Calibration counts on 2026-04
-----------------------------
* 25 scheme detail pages detected
* 14 PTR rows extracted (Large Cap + Flexi Cap + Large & Mid Cap +
  Multicap + Midcap + Small Cap + ELSS + Healthcare + Retirement +
  Aggressive Hybrid + Arbitrage + Equity Savings + Balanced Advantage +
  Multi Asset) — the 11 ranked equity schemes from scheme_master all
  produce PTR rows, plus Retirement Fund + Arbitrage + Multi Asset
  Allocation which the master tracks but don't carry a canonical
  category.  The 3 FoF schemes and 8 debt schemes plus the target-
  maturity index legitimately omit PTR.

Holdings extraction is deferred to the parallel Excel-based ISIN-tagged
path (Phase 3.A) — the factsheet portfolio table prints company name +
percent-to-NAV but no ISIN.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pdfplumber

from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)


_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


# ---------------------------------------------------------------------------
# Scheme-name extraction (word positions in the top-left logo band)
# ---------------------------------------------------------------------------


def _scheme_title_from_words(page) -> str | None:
    """Return the printed scheme name from the top-left logo band, or
    ``None`` for non-scheme pages.

    Strategy: locate the ``PGIM`` word in the top-left band (top < 110,
    x0 < 230), then collect all uppercase tokens (plus ``&``, ``-``, and
    digits) in the y-window [y_pgim - 3, y_pgim + 40] in reading order.
    Stops at the first ``FUND`` token (with a special-case to allow
    ``OF FUND`` continuation for fund-of-fund schemes).
    """
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None
    band = [w for w in words if 25 < w["top"] < 110 and w["x0"] < 230]
    if not band:
        return None
    band.sort(key=lambda w: (w["top"], w["x0"]))
    # Locate the leading "PGIM" anchor.
    pgim = None
    for w in band:
        if w["text"] == "PGIM":
            pgim = w
            break
    if pgim is None:
        return None
    y_pgim = pgim["top"]
    # Collect candidate tokens within the title window.
    tokens = [
        (w["top"], w["x0"], w["text"])
        for w in band
        if y_pgim - 3 <= w["top"] <= y_pgim + 40 and w["x0"] < 230
    ]
    tokens.sort()
    title_words: list[str] = []
    seen_fund = False
    for _top, _x, t in tokens:
        if seen_fund:
            # After the first FUND, only allow OF + FUND (FoF tail).
            if t == "OF":
                title_words.append(t)
                continue
            if t == "FUND":
                title_words.append(t)
                break
            # Anything else (single-letter description fragment, etc.)
            # terminates the title.
            break
        if re.fullmatch(r"[A-Z]+", t) or t in ("&", "-"):
            title_words.append(t)
            if t == "FUND":
                seen_fund = True
            continue
        if re.fullmatch(r"\d+", t):
            # Year tokens like 2028 in
            # ``CRISIL IBX GILT INDEX - APR 2028 FUND``.
            title_words.append(t)
            continue
        # Mixed-case word (e.g. start of the description line) → stop.
        break
    # Drop a trailing lone hyphen if it slipped in.
    while title_words and title_words[-1] == "-":
        title_words.pop()
    if not title_words:
        return None
    # Sanity: title must start with PGIM INDIA.
    if len(title_words) < 3 or title_words[0] != "PGIM" or title_words[1] != "INDIA":
        return None
    return " ".join(title_words)


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# PGIM prints PTR as ``Portfolio Turnover: 0.43`` (already a fraction,
# stored as "times").  Hybrid pages append a parenthetical scope tag
# ``(For Equity)`` after the value; we match the numeric token only.
# Storage convention is also fraction → no unit conversion needed.
_PTR_TEXT_RE = re.compile(
    r"Portfolio\s+Turnover\s*:\s*([0-9]+\.[0-9]+)",
    re.IGNORECASE,
)


@register_adapter
class PgimIndiaAdapter(ManagerAdapter):
    """PGIM India Mutual Fund factsheet adapter.

    ``amc_slug = 'pgim_india'`` matches ``scheme_master.amc_code``
    exactly, so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "pgim_india"
    source_label = "PGIM India Mutual Fund"

    # -------------------------------------------------------------------
    # URL
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the canonical PGIM India factsheet PDF URL for data
        month ``ym='YYYY-MM'``.

        The URL is deterministic and embeds the **data month** name in
        Title Case + four-digit year, with spaces URL-encoded as
        ``%20``.  No API lookup is required — the AMC's CMS exposes
        every published month under the same path.
        """
        y, m = map(int, ym.split("-"))
        month_name = _MONTH_NAMES[m - 1]
        filename = f"Factsheet - {month_name} {y}.pdf"
        encoded = filename.replace(" ", "%20")
        return (
            "https://www.pgimindia.com/api/v1/brochure/about-us/image/" + encoded
        )

    # -------------------------------------------------------------------
    # PTR
    # -------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                scheme = _scheme_title_from_words(page)
                if not scheme:
                    continue
                text = page.extract_text() or ""
                m = _PTR_TEXT_RE.search(text)
                if not m:
                    continue
                try:
                    ptr_value = float(m.group(1))
                except ValueError:
                    continue
                # Fail-fast: drop NaN / non-positive.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )

    # -------------------------------------------------------------------
    # Holdings — deferred.  PGIM's portfolio listing prints company
    # name + ``% to Net Assets`` in a two-column layout with sector
    # banners but no ISINs.  Phase 3.A's ISIN-tagged Excel path covers
    # PGIM for the portfolio-overlap metric.  We yield nothing here.
    # -------------------------------------------------------------------

    def parse_holdings(
        self, pdf_path: Path, ym: str
    ) -> Iterable[ParsedHoldingRecord]:
        return ()
