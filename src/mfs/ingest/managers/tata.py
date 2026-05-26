"""Tata Mutual Fund — factsheet adapter (Phase 3.B).

Calibrated against the April 2026 combined factsheet PDF
(``data/raw/factsheets/tata/2026-04.pdf``, ~9.76MB, 126 pages, 68 scheme
pages plus cover / TOC / riskometer / market commentary / IDCW history
/ TER tables / disclosures at the back).

URL discovery
-------------
``www.tatamutualfund.com`` is a Next.js SPA whose factsheet downloads
page (``/information-documents/factsheets``) loads a JSON manifest that
points at the canonical PDF on the AMC's CMS host
``betacms.tatamutualfund.com``. For April-2026 the manifest emits

    field_month   = "Apr"
    field_year    = "2026"
    field_media_document =
       "https://betacms.tatamutualfund.com/system/files/2026-05/
        Tata MF Fact Sheet - April 2026.pdf"

so the canonical URL pattern is:

    https://betacms.tatamutualfund.com/system/files/<publish-YYYY-MM>/
    Tata%20MF%20Fact%20Sheet%20-%20<MonthName>%20<YYYY>.pdf

where the folder is the **publish month** (data month + 1) and the
filename embeds the **data month** name + four-digit year. Spaces in
the filename are URL-encoded as ``%20``.

Layout findings driving the parser
----------------------------------
* Scheme pages have a clean first non-empty line ``Tata <Name> Fund``
  (or ``Tata <Name> FOF`` for the FoF schemes, ``Tata <Name> Exchange
  Traded Fund`` for ETFs). A handful of pages render the title in
  ALL CAPS (``TATA NIFTY 50 INDEX FUND``, ``TATA NIFTY 50 EXCHANGE
  TRADED FUND``, ``TATA Nifty500 Multicap India Manufacturing
  50:30:20 Index Fund``, ``TATA Nifty G Sec Dec 2029 Index Fund``);
  case is preserved on the printed-name field (canonicalize() in the
  fuzzy match uppercases both sides anyway).

* PTR is printed as a **percent** in the left column on equity / hybrid
  / arbitrage / index pages:

      ``TURNOVER``
      ``Portfolio Turnover (Equity component only) : 50.03%``

  Tata stores it as a percent, so we divide by 100 to keep the
  fraction storage convention (50.03% -> 0.5003). pdfplumber's
  ``extract_text`` mangles the line on column-bleed pages (Balanced
  Advantage, Arbitrage); we use word-position extraction throughout to
  side-step that. The numeric token sits in the left column at
  x0 ~ 140-150 (right of the ``:`` glyph at x0 ~ 118) within a 15pt
  vertical window of the ``Portfolio`` ``Turnover`` words. Pure debt
  / overnight / liquid / FOF pages either print ``Portfolio Turnover
  (Total) : NA`` or omit the block entirely — those legitimately have
  no PTR and yield nothing.

Calibration counts on 2026-04
-----------------------------
* 68 scheme pages detected
* 33 PTR rows extracted (equity / hybrid / index / ETF / arbitrage
  pages — debt / FOF schemes don't print the PTR block)
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


def _publish_ym(data_ym: str) -> tuple[int, int]:
    """Publish month = data month + 1 (with year rollover)."""
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return y + 1, 1
    return y, m + 1


# A first-line scheme-name match. We accept any first non-empty line
# whose leading token is ``Tata`` (case-insensitive) so the ALL-CAPS
# variants (``TATA NIFTY 50 INDEX FUND``) are matched alongside the
# title-case ones. We do NOT require the line to end in Fund / FOF /
# ETF because some titles wrap into a second visual row — instead the
# caller filters non-scheme pages by requiring the FUND SIZE / Portfolio
# Turnover anchor to be present.
_SCHEME_FIRST_LINE_RE = re.compile(r"^TATA\s", re.IGNORECASE)

# A bare-number percent token (left-column PTR value).
_PTR_PCT_RE = re.compile(r"^(\d+(?:\.\d+)?)%$")


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from the first non-empty line, or
    None for non-scheme pages (cover / TOC / riskometer / market
    commentary / disclosures / IDCW history / TER tables / back-cover).
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not _SCHEME_FIRST_LINE_RE.match(first):
        return None
    # Reject TOC-style lines ``Tata India Consumer Fund 55`` that bleed
    # in on the index page (the TOC entries end with a page number).
    if re.search(r"\s\d{1,3}$", first):
        return None
    # Drop a footnote symbol the Tata factsheet sometimes appends after
    # the Fund suffix on a few schemes (defensive — none observed in
    # 2026-04 but documented in older releases).
    first = first.rstrip("*").rstrip("$").rstrip("#").strip()
    # Collapse multiple whitespace runs.
    first = re.sub(r"\s+", " ", first)
    return first or None


def _find_ptr_pct(words: list[dict]) -> float | None:
    """Return the printed Portfolio Turnover **percent**, or None.

    Anchors on the ``Portfolio`` + ``Turnover`` word pair in the left
    column (x0 < 100, same y-band), then scans within a 15pt vertical
    window for a ``XX.XX%`` token at x0 ~ 140-170. The label's value
    column is consistently positioned at x0 ~ 142-150 across all
    Tata scheme pages observed.
    """
    anchor: dict | None = None
    for i, w in enumerate(words):
        if w["text"] != "Portfolio":
            continue
        if w["x0"] >= 100:
            continue
        for j in range(i + 1, min(i + 6, len(words))):
            v = words[j]
            if v["text"] != "Turnover":
                continue
            if abs(v["top"] - w["top"]) > 3:
                continue
            if not (w["x1"] < v["x0"] < w["x1"] + 30):
                continue
            anchor = w
            break
        if anchor is not None:
            break
    if anchor is None:
        return None
    anchor_y = anchor["top"]
    for w in words:
        dy = w["top"] - anchor_y
        if not (-2 < dy < 15):
            continue
        if not (100 < w["x0"] < 175):
            continue
        m = _PTR_PCT_RE.match(w["text"])
        if not m:
            continue
        try:
            return float(m.group(1))
        except ValueError:
            continue
    return None


@register_adapter
class TataAdapter(ManagerAdapter):
    """Tata Mutual Fund factsheet adapter.

    ``amc_slug`` matches ``scheme_master.amc_code`` directly (``tata``),
    so no alias entry is needed in ``_scheme_match.py``.
    """

    amc_slug = "tata"
    source_label = "Tata Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical Tata combined factsheet PDF URL for data
        month ym='YYYY-MM'.

        Tata publishes the previous month's factsheet on its CMS host
        ``betacms.tatamutualfund.com`` one calendar month after the data
        month (April-2026 data is published under the 2026-05 folder).
        The filename embeds the **data month** name + four-digit year,
        with spaces URL-encoded as ``%20``.
        """
        y, m = map(int, ym.split("-"))
        publish_year, publish_month = _publish_ym(ym)
        month_name = _MONTH_NAMES[m - 1]
        filename = f"Tata MF Fact Sheet - {month_name} {y}.pdf"
        encoded = filename.replace(" ", "%20")
        return (
            "https://betacms.tatamutualfund.com/system/files/"
            f"{publish_year:04d}-{publish_month:02d}/{encoded}"
        )

    # -----------------------------------------------------------------
    # PTR
    # -----------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                # ``extract_text`` column-bleeds the PTR line on hybrid /
                # arbitrage / balanced-advantage pages so the literal
                # ``Portfolio Turnover`` string is mangled. Gate on the
                # broader ``TURNOVER`` section header (always intact) and
                # the presence of ``Portfolio`` (the anchor word that
                # ``_find_ptr_pct`` searches for).
                if "TURNOVER" not in text or "Portfolio" not in text:
                    continue
                try:
                    words = page.extract_words(use_text_flow=True)
                except Exception:  # noqa: BLE001
                    continue
                pct = _find_ptr_pct(words)
                if pct is None:
                    continue
                # Drop NaN / non-positive (Tata prints a real percent or
                # ``NA`` — a zero PTR is implausible on the equity /
                # arbitrage pages that carry the label).
                if pct != pct or pct <= 0:
                    continue
                ptr_fraction = pct / 100.0
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_fraction,
                    source_amc=self.amc_slug,
                )

    # -----------------------------------------------------------------
    # Holdings — deferred. Tata's portfolio table is a two-column
    # ``Company Name / No. of Shares / Market Value Rs.Lakhs / % of
    # Assets`` layout with sector banners interleaved between rows.
    # pdfplumber's ``extract_text`` heavily column-bleeds this section
    # (visible in the calibration dumps above), so robust extraction
    # would need its own x-band calibration. Phase 3.A's ISIN-tagged
    # Excel path covers Tata for the portfolio overlap metric, so we
    # return () here.
    # -----------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
