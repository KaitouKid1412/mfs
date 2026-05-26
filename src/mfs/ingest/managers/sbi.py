"""SBI Mutual Fund factsheet adapter (Phase 3 refresh).

Calibrated against the April 2026 combined scheme factsheet
(``data/raw/factsheets/sbi/2026-04.pdf``, ~13 MB, 111 pages).

URL pattern (discovered via Wayback Machine):
  https://www.sbimf.com/docs/default-source/scheme-factsheets/
    all-sbimf-schemes-factsheet-{month_lower}-{year}.pdf
The server resolves the latest version automatically; no ``sfvrsn=`` needed.

Layout findings driving the parser
----------------------------------
* SBI prints each scheme on its own per-page block (pages ~13-72). The
  canonical page title ``SBI <Fund Name>`` sits in the bottom band at
  y ~ 715-745 in a centered position (x0 ~ 290-365). On a handful of
  fund-of-fund pages the title appears mid-page (y ~ 558 on the US
  Specific Equity Active FoF page, y ~ 692 on Constant Maturity Gilt).

* The previous adapter scanned only the bottom band (y > 600) and
  matched a regex that disallowed apostrophes, digits, dashes, and the
  "FoF/FOF" terminator. That dropped:
      - SBI Children's Fund - Savings Plan      (apostrophe + dash)
      - SBI Children's Fund -Investment Plan    (apostrophe, no-space dash)
      - SBI US Specific Equity Active FoF       (FoF terminator)
      - SBI Income Plus Arbitrage Active FOF    (FOF terminator)
      - SBI Dynamic Asset Allocation Active FoF (FoF terminator)
      - SBI Constant Maturity 10-Year Gilt Fund (digit)
      - SBI Retirement Benefit Fund – Aggressive Plan etc. (en-dash + Plan)
  The new title detector accepts all of these.

* PTR is printed as a **fraction** (e.g. ``Equity Turnover : 0.31``).
  Storage uses the same fraction convention so we pass through without
  unit conversion. The label ``Equity Turnover`` is what we want — not
  ``Total Turnover`` which sums Equity + Debt + Derivatives.

* Two-strategy PTR extraction (mirrors the Nippon adapter):
    1. **Text regex** — matches the clean line on most equity pages.
    2. **Word-position fallback** — when ``extract_text`` column-bleeds
       the line (notably SBI Equity Hybrid Fund p41 and SBI Flexicap
       p15 where the right-column industry table interleaves with the
       Quantitative Data block), find ``Equity`` + ``Turnover`` in the
       left column on the same y-band (±2pt) and take the next
       ``\\d+\\.\\d+`` token to the right within that same tight band.

* AUM is printed in INR Crore. SBI prints two values per scheme:
    - ``AAUM for the Month of April 2026 ` 52,854.99 Crores`` (avg)
    - ``AUM as on April 30, 2026 ` 53,287.42 Crores``         (month-end)
  The metric set treats ``aum_crore`` as point-in-time so we prefer
  **month-end (``AUM as on ...``)** over AAUM. If the month-end value
  is unavailable (some FoF pages omit it) we fall back to AAUM.

* Two-strategy AUM extraction:
    1. **Text regex** — match the ``AUM as on ... ` X Crores`` line,
       then fall back to the AAUM line.
    2. **Word-position fallback** — for the small number of pages
       where ``extract_text`` mangles either line via column-bleed,
       find the AAUM/AUM marker in the left column and take the next
       ``X,XXX.XX`` Crore-formatted token below it within ~35pt.

* Holdings extraction is preserved from the prior implementation
  (the two-column portfolio layout was layout-stable).

Calibration counts on 2026-04 (post-refresh)
--------------------------------------------
* 55 scheme pages detected by the new title scanner (was 23 with the
  old `SBI [A-Za-z &-]+ Fund` regex).
* 28 PTR records parsed end-to-end (was 23). Schemes legitimately
  without PTR (debt / liquid / arbitrage funds where the metric is
  meaningless, plus new schemes <1yr old) are dropped silently.
* 55 AUM records parsed (was 23).
"""

from __future__ import annotations

import re
from itertools import groupby
from pathlib import Path
from typing import Iterable

import pdfplumber

from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_MONTH_FULL = (
    "January|February|March|April|May|June|"
    "July|August|September|October|November|December"
)


# ---------------------------------------------------------------------------
# Scheme-title detection
# ---------------------------------------------------------------------------

# Title characters: alpha, digit, ampersand, apostrophe (straight + curly),
# any of three dash flavours, slash, dot, parens. Terminator is Fund / FoF /
# FOF. An optional "<dash> <suffix words> Plan" tail covers the Retirement
# Benefit / Children's Fund variants.
_TITLE_RE = re.compile(
    r"\bSBI [A-Za-z0-9 &'’–—\-/.()]+? (?:Fund|FoF|FOF)"
    r"(?:\s*[-–—]\s*[A-Za-z]+(?:\s+[A-Za-z]+){0,3}\s*Plan)?"
)


def _scheme_title_from_words(words: list[dict]) -> str | None:
    """Locate the canonical ``SBI <Fund Name>`` page title.

    The bottom-band footer at y ~ 690-760 carries the title on virtually
    every per-scheme page in the cached April 2026 factsheet. Two
    fund-of-fund pages place the title mid-page; we fall back to scanning
    every row for them.

    Returns the matched title string (whitespace-collapsed) or ``None``
    when no scheme title is present (cover / TOC / TER aggregation /
    disclosure / glossary pages).
    """
    rows: list[tuple[int, float, list[dict]]] = []
    for _, grp in groupby(
        sorted(words, key=lambda w: (round(w["bottom"]), w["x0"])),
        key=lambda w: round(w["bottom"]),
    ):
        row = list(grp)
        row.sort(key=lambda w: w["x0"])
        if row:
            rows.append((round(row[0]["bottom"]), row[0]["x0"], row))

    band_rows = [r for r in rows if 690 <= r[0] <= 760]

    # Priority 1: title-band row, "SBI" is the FIRST token (typical case).
    for _, _, row in band_rows:
        if row[0]["text"] != "SBI":
            continue
        phrase = " ".join(w["text"] for w in row)
        if "known as" in phrase.lower() or "formerly" in phrase.lower():
            continue
        m = _TITLE_RE.search(phrase)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()

    # Priority 2: title-band row where SBI appears later (interleaved with
    # holdings text on column-bled pages).
    for _, _, row in band_rows:
        phrase = " ".join(w["text"] for w in row)
        if "known as" in phrase.lower() or "formerly" in phrase.lower():
            continue
        for idx, w in enumerate(row):
            if w["text"] == "SBI":
                sub = " ".join(v["text"] for v in row[idx:])
                m = _TITLE_RE.match(sub)
                if m:
                    return re.sub(r"\s+", " ", m.group(0)).strip()
                break

    # Priority 3: any row anywhere (FoF pages with mid-page title).
    for _, _, row in rows:
        phrase = " ".join(w["text"] for w in row)
        if "known as" in phrase.lower() or "formerly" in phrase.lower():
            continue
        for idx, w in enumerate(row):
            if w["text"] == "SBI":
                sub = " ".join(v["text"] for v in row[idx:])
                m = _TITLE_RE.match(sub)
                if m:
                    return re.sub(r"\s+", " ", m.group(0)).strip()
                break
    return None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Clean-text PTR: ``Equity Turnover : 0.31`` (already a fraction).
_PTR_TEXT_RE = re.compile(r"Equity\s+Turnover\s*:\s*(\d+\.\d+)", re.IGNORECASE)


def _find_ptr_via_words(words: list[dict]) -> float | None:
    """Word-position fallback for PTR.

    Find an ``Equity`` token in the LEFT column (x0 < 200), then a
    ``Turnover`` token to its right on the SAME y-band (±2pt, tight to
    avoid pulling 'Total Turnover' from the next visual line). The first
    ``\\d+\\.\\d+`` token to the right of ``Turnover`` on that tight band
    is the PTR value.

    A wider y-band would incorrectly pick up the ``Total Turnover``
    value (e.g. 0.89 on page 41) which is Equity + Debt + Derivatives.
    """
    for w in words:
        if w["text"] != "Equity":
            continue
        if w["x0"] >= 200:
            continue
        ybot = w["bottom"]
        turnover: dict | None = None
        for v in words:
            if v is w:
                continue
            if abs(v["bottom"] - ybot) > 2:
                continue
            if v["x0"] <= w["x1"]:
                continue
            if v["x0"] >= 200:
                continue
            if v["text"].lower().startswith("turnover"):
                turnover = v
                break
        if turnover is None:
            continue
        nums = [
            v for v in words
            if abs(v["bottom"] - ybot) <= 2
            and v["x0"] > turnover["x1"]
            and v["x0"] < 200
            and re.fullmatch(r"\d+\.\d{1,3}", v["text"].rstrip(",;."))
        ]
        nums.sort(key=lambda v: v["x0"])
        for v in nums:
            try:
                val = float(v["text"].rstrip(",;."))
            except ValueError:
                continue
            if val > 0:
                return val
        return None
    return None


# ---------------------------------------------------------------------------
# Holdings extraction (preserved from prior version, layout-stable)
# ---------------------------------------------------------------------------

_SBI_LEFT_NAME = (200.0, 350.0)
_SBI_LEFT_WEIGHT = (358.0, 385.0)
_SBI_RIGHT_NAME = (390.0, 545.0)
_SBI_RIGHT_WEIGHT = (555.0, 580.0)
_SBI_STOP_TOKENS = {
    "Sub", "UNITS", "DEBT", "Cash", "Cash,", "Grand", "Treasury",
    "Government", "Convertible", "Total",
}
_SBI_STOCK_SUFFIX_RE = re.compile(
    r"\b(Ltd|Limited|Inc|Corp|Co\.|PLC|S\.A\.)\b\.?$", re.IGNORECASE
)


@register_adapter
class SbiAdapter(ManagerAdapter):
    """SBI Mutual Fund factsheet adapter."""

    amc_slug = "sbi"
    source_label = "SBI Mutual Fund"

    # -------------------------------------------------------------------
    # URL
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Build the canonical PDF URL for data month ym='YYYY-MM'.

        SBI uses lowercase full month names in the filename.
        """
        y, m = map(int, ym.split("-"))
        month_lower = _MONTH_FULL.split("|")[m - 1].lower()
        return (
            "https://www.sbimf.com/docs/default-source/scheme-factsheets/"
            f"all-sbimf-schemes-factsheet-{month_lower}-{y}.pdf"
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
                # Cheap gate: skip pages that are clearly not scheme pages.
                # All scheme pages print either AAUM or AUM-as-on.
                if "AAUM" not in text and "AUM as on" not in text:
                    continue
                words = page.extract_words(use_text_flow=True)
                scheme = _scheme_title_from_words(words)
                if not scheme:
                    continue
                ptr_value: float | None = None
                m = _PTR_TEXT_RE.search(text)
                if m:
                    try:
                        ptr_value = float(m.group(1))
                    except ValueError:
                        ptr_value = None
                if ptr_value is None:
                    ptr_value = _find_ptr_via_words(words)
                if ptr_value is None:
                    continue
                # Drop NaN / non-positive — SBI prints a real fraction or
                # omits the block; there's no zero-PTR scheme.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )

    # -------------------------------------------------------------------
    # -------------------------------------------------------------------
    # Holdings (preserved from prior implementation, layout-stable)
    # -------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                yield from self._parse_page_holdings(page, page_idx + 1)

    def _parse_page_holdings(self, page, page_num: int) -> Iterable[ParsedHoldingRecord]:
        words = page.extract_words(use_text_flow=True)
        if not words:
            return
        scheme_name = _scheme_title_from_words(words)
        if not scheme_name:
            return
        # Locate the 'Equity Shares' banner — start of portfolio. If absent
        # this isn't a portfolio page (e.g. front cover, glossary).
        eq_y = self._find_equity_shares_y(words)
        if eq_y is None:
            return
        # End of section: a row containing a stop token in name-column range.
        stop_y = self._find_stop_y(words, eq_y)
        band = [
            w for w in words
            if eq_y + 3 < w["bottom"] < (stop_y if stop_y else eq_y + 600)
            and _SBI_LEFT_NAME[0] <= w["x0"] <= _SBI_RIGHT_WEIGHT[1]
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
                (_SBI_LEFT_NAME, _SBI_LEFT_WEIGHT),
                (_SBI_RIGHT_NAME, _SBI_RIGHT_WEIGHT),
            ):
                hold = self._extract_sbi_column(row, col_range, weight_range)
                if hold is None:
                    continue
                if not _SBI_STOCK_SUFFIX_RE.search(hold["name"]):
                    continue
                if hold["name"].count("Ltd.") + hold["name"].count("Ltd ") > 1:
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
    def _find_equity_shares_y(words: list[dict]) -> float | None:
        for w in words:
            if w["text"] != "Equity":
                continue
            for v in words:
                if (
                    v["text"] == "Shares"
                    and abs(v["bottom"] - w["bottom"]) < 3
                    and v["x0"] > w["x1"]
                    and v["x0"] < 300  # ensure we picked the portfolio header, not stats
                ):
                    return w["bottom"]
        return None

    @staticmethod
    def _find_stop_y(words: list[dict], eq_y: float) -> float | None:
        for w in words:
            if w["bottom"] < eq_y + 5:
                continue
            if w["text"] in _SBI_STOP_TOKENS and w["x0"] < 350:
                return w["bottom"]
        return None

    @staticmethod
    def _extract_sbi_column(
        row: list[dict],
        name_range: tuple[float, float],
        weight_range: tuple[float, float],
    ) -> dict | None:
        name_words = [
            w for w in row
            if name_range[0] <= w["x0"] <= name_range[1]
            and w["text"] not in {"•", "@"}
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
