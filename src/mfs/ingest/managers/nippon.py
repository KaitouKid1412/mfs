"""Nippon India Mutual Fund — factsheet adapter (Phase 3.D rewrite).

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/nippon/2026-04.pdf``, ~21 MB, 164 pages, 108 scheme
pages plus cover / TOC / IDCW history / TER tables / disclosures).

URL discovery
-------------
Nippon India publishes the monthly factsheet under

    https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/
    Nippon-FS-{MMM-uppercase}-{YYYY}.pdf

where ``MMM`` is the **data** month's 3-letter abbreviation in UPPER case
and ``YYYY`` is the data month's year. Older months (>~3 months back) move
to ``/InvestorServices/FactSheets/`` with title-case month — not handled
here because the orchestrator always targets the latest disclosure.

Layout findings driving the parser
----------------------------------
* Scheme pages start with the printed scheme name on the **first**
  non-empty line, prefixed ``Nippon India``. Cover, TOC, market
  commentary and the TER aggregation pages (110-114) all start with
  other content. We require the first line to match
  ``^Nippon India .+`` AND to NOT be a TER aggregation page where two
  scheme names appear on the same line (these report multi-fund TER
  tables, not per-scheme PTR/AUM — we drop them).

* PTR is printed as a **fraction** (Nippon stores it directly, no need
  to divide by 100). The label is ``Portfolio Turnover (Times)`` —
  occasionally with no space between ``Turnover`` and ``(Times)`` (e.g.
  page 41 prints ``Portfolio Turnover(Times) 0.32``). Pages without a
  PTR (debt / liquid / overnight / ETF / FoF) print either
  ``Portfolio Turnover (Times) --`` (a literal double-dash) or omit the
  block entirely. We drop both.

* Two-strategy extraction for PTR:
    1. **Text regex** (fast path) — matches the clean line on most
       equity / hybrid / index pages.
    2. **Word-position fallback** — when ``extract_text`` column-bleeds
       the line (none observed in 2026-04 but defensive against future
       layout regressions), find the words ``Portfolio`` + ``Turnover``
       in the left column and take the next ``\\d+\\.\\d+`` token on the
       same y-band.

* Holdings extraction (preserved from prior version): two-column
  portfolio layout with sector banners and footnote glyphs (£, †, ‡,
  #, *) that must be stripped. Confined to x-bands 215-575.

Calibration counts on 2026-04 (post-rewrite)
--------------------------------------------
* 108 scheme pages detected
* 55 PTR records parsed (the rest are debt / liquid / ETF pages that
  legitimately omit the PTR block)
* After scheme-name fuzzy matching + Bonus Option filter, roughly 40
  PTR rows persist (the ETF schemes whose names lost the "Reliance"
  historical prefix in scheme_master still fail the match — that's a
  scheme_master coverage gap, not a parser issue)
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

_MONTH_ABBR_UPPER = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)

# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------

# A scheme page's first non-empty line is "Nippon India <name>" — possibly
# wrapping into a second visual line. We accept the first line as-is so
# segregated portfolio markers like
#   ``Nippon India Credit Risk Fund (Existing Number of Segregated Portfolios - 1)``
# remain intact. Pages 110-114 are TER aggregation tables whose first line
# concatenates two unrelated scheme names (a multi-column TER table) — we
# drop them by requiring the line not to contain ``Nippon India`` twice.
_NIPPON_PREFIX = "Nippon India "


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from the first non-empty line, or
    ``None`` for non-scheme pages (cover / TOC / market commentary / TER
    aggregation / disclosures).
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.startswith(_NIPPON_PREFIX):
        return None
    # Drop TER aggregation pages (110-114): two scheme names concatenated
    # on the same first line.
    if first.count(_NIPPON_PREFIX) > 1:
        return None
    # Collapse multiple whitespace runs.
    first = re.sub(r"\s+", " ", first).strip()
    return first or None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Nippon prints PTR as a **fraction** (e.g. ``0.27`` = 27%). Storage uses
# the same fraction convention so we pass through without unit conversion.
# The regex accepts an optional space between ``Turnover`` and ``(Times)``
# (page 41 omits it).
_PTR_TEXT_RE = re.compile(
    r"Portfolio\s+Turnover\s*\(Times\)\s+(\d+\.\d+)",
    re.IGNORECASE,
)

# Sentinel printed on pages without a meaningful PTR (e.g. p20 US Equity
# Opportunities Fund prints ``Portfolio Turnover (Times) --``). We don't
# need to match this -- the numeric regex naturally fails -- but the
# fallback path checks for the sentinel and bails out.
_PTR_SENTINEL_RE = re.compile(
    r"Portfolio\s+Turnover\s*\(Times\)\s*--",
    re.IGNORECASE,
)


def _find_ptr_via_words(page) -> float | None:
    """Word-position fallback for PTR extraction.

    Locates the ``Portfolio`` + ``Turnover`` word pair in the left
    column (x0 < 250) on the same y-band, then takes the next
    ``\\d+\\.\\d+`` token to the right of ``Turnover`` within a 15pt
    vertical window. Used when ``extract_text`` column-bleeds the line.
    """
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None
    for i, w in enumerate(words):
        if w["text"] != "Portfolio":
            continue
        if w["x0"] >= 250:
            continue
        # Find adjacent 'Turnover' word.
        turnover: dict | None = None
        for j in range(i + 1, min(i + 8, len(words))):
            v = words[j]
            if abs(v["bottom"] - w["bottom"]) > 3:
                continue
            if v["text"].lower().startswith("turnover"):
                turnover = v
                break
        if turnover is None:
            continue
        # Find first numeric (X.YY) token to the right within the same band.
        for n in words:
            if abs(n["bottom"] - turnover["bottom"]) > 5:
                continue
            if n["x0"] <= turnover["x1"]:
                continue
            txt = n["text"].rstrip(",;.")
            if re.fullmatch(r"\d+\.\d{1,3}", txt):
                try:
                    val = float(txt)
                except ValueError:
                    continue
                if val <= 0:
                    return None
                return val
        return None
    return None


# ---------------------------------------------------------------------------
# Holdings (preserved from prior version, layout-stable)
# ---------------------------------------------------------------------------

_NIP_LEFT_NAME = (215.0, 370.0)
_NIP_LEFT_WEIGHT = (370.0, 395.0)
_NIP_RIGHT_NAME = (395.0, 545.0)
_NIP_RIGHT_WEIGHT = (548.0, 575.0)
_NIP_STOP_TOKENS = {"Cash", "Cash,", "Sub", "Grand", "Other", "Net"}
_NIP_STOCK_SUFFIX_RE = re.compile(
    r"\b(Ltd|Limited|Inc|Corp|Co\.|PLC|S\.A\.)\b\.?$", re.IGNORECASE
)


@register_adapter
class NipponAdapter(ManagerAdapter):
    """Nippon India Mutual Fund factsheet adapter."""

    amc_slug = "nippon"
    source_label = "Nippon India Mutual Fund"

    # -------------------------------------------------------------------
    # URL
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the canonical Nippon India factsheet PDF URL for data
        month ``ym='YYYY-MM'``.

        Nippon publishes under ``/FactSheetsDocuments/`` with an upper-
        case 3-letter month abbreviation matching the **data** month.
        """
        y, m = map(int, ym.split("-"))
        month_abbr = _MONTH_ABBR_UPPER[m - 1]
        return (
            "https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/"
            f"Nippon-FS-{month_abbr}-{y:04d}.pdf"
        )

    # -------------------------------------------------------------------
    # PTR
    # -------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                # Skip pages that print the no-PTR sentinel.
                if _PTR_SENTINEL_RE.search(text):
                    continue
                ptr_value: float | None = None
                m = _PTR_TEXT_RE.search(text)
                if m:
                    try:
                        ptr_value = float(m.group(1))
                    except ValueError:
                        ptr_value = None
                if ptr_value is None:
                    # Only invoke the word-position fallback when the
                    # label appears on the page; saves an extract_words
                    # call on the many pure-debt / liquid pages that
                    # omit PTR entirely.
                    if "Portfolio" in text and "Turnover" in text:
                        ptr_value = _find_ptr_via_words(page)
                if ptr_value is None:
                    continue
                # Drop NaN / non-positive — Nippon prints a real
                # fraction or a sentinel; there's no zero-PTR scheme.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )

    # -------------------------------------------------------------------
    # Holdings extraction (preserved from prior implementation)
    # -------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                yield from self._parse_page_holdings(page, page_idx + 1)

    def _parse_page_holdings(self, page, page_num: int) -> Iterable[ParsedHoldingRecord]:
        text = page.extract_text() or ""
        scheme_name = _scheme_name_from_page(text)
        if not scheme_name:
            return
        try:
            words = page.extract_words(use_text_flow=True)
        except Exception:  # noqa: BLE001
            return
        portfolio_y = self._find_portfolio_header_y(words)
        if portfolio_y is None:
            return
        stop_y = self._find_stop_y(words, portfolio_y)
        band = [
            w for w in words
            if portfolio_y + 3 < w["bottom"] < (stop_y if stop_y else portfolio_y + 700)
            and _NIP_LEFT_NAME[0] <= w["x0"] <= _NIP_RIGHT_WEIGHT[1]
        ]
        band.sort(key=lambda w: (w["bottom"], w["x0"]))
        rows: list[list[dict]] = []
        for w in band:
            if rows and abs(w["bottom"] - rows[-1][-1]["bottom"]) < 3:
                rows[-1].append(w)
            else:
                rows.append([w])
        for row in rows:
            for col_range, weight_range in (
                (_NIP_LEFT_NAME, _NIP_LEFT_WEIGHT),
                (_NIP_RIGHT_NAME, _NIP_RIGHT_WEIGHT),
            ):
                hold = self._extract_nip_column(row, col_range, weight_range)
                if hold is None:
                    continue
                if not _NIP_STOCK_SUFFIX_RE.search(hold["name"]):
                    continue
                if hold["name"].count("Ltd.") + hold["name"].count("Ltd ") + hold["name"].count("Limited") > 1:
                    continue
                if len(hold["name"].split()) > 7:
                    continue
                yield ParsedHoldingRecord(
                    scheme_name_printed=scheme_name,
                    security_name=hold["name"],
                    weight_pct=hold["weight"],
                    isin=None,
                    instrument_type="Equity",
                    source_amc=self.amc_slug,
                )

    @staticmethod
    def _find_portfolio_header_y(words: list[dict]) -> float | None:
        for w in words:
            if "Company" in w["text"] and "Issuer" in w["text"]:
                return w["bottom"]
            if w["text"] == "Company/Issuer":
                return w["bottom"]
        return None

    @staticmethod
    def _find_stop_y(words: list[dict], header_y: float) -> float | None:
        for w in words:
            if w["bottom"] < header_y + 5:
                continue
            if w["text"] in _NIP_STOP_TOKENS and w["x0"] < 350:
                return w["bottom"]
        return None

    @staticmethod
    def _extract_nip_column(
        row: list[dict],
        name_range: tuple[float, float],
        weight_range: tuple[float, float],
    ) -> dict | None:
        name_words = [
            w for w in row
            if name_range[0] <= w["x0"] <= name_range[1]
            and w["text"] not in {"•", "@", "*"}
        ]
        weight_words = [
            w for w in row
            if weight_range[0] <= w["x0"] <= weight_range[1]
        ]
        if not name_words or not weight_words:
            return None
        name_words.sort(key=lambda w: w["x0"])
        name = " ".join(w["text"] for w in name_words).strip()
        name = re.sub(r"[£@†‡#*]", "", name)
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            return None
        for w in weight_words:
            t = w["text"].rstrip("%").replace(",", "")
            try:
                weight = float(t)
            except ValueError:
                continue
            return {"name": name, "weight": weight}
        return None
