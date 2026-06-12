"""HDFC Mutual Fund — factsheet adapter (refreshed for PTR/AUM coverage).

Calibrated against the April 2026 factsheet PDF
(https://files.hdfcfund.com/s3fs-public/2026-05/HDFC%20MF%20Factsheet%20-%20April%202026.pdf,
6.0 MB, 140 pages, ~59 first scheme pages plus 44 "Contd from previous page"
continuation pages, plus disclosure / TER / NAV index pages).

Layout findings driving the parser
----------------------------------
* Scheme pages start with the printed scheme name on the **first** non-empty
  line, prefixed ``HDFC ``. Continuation pages also start with the same name
  but follow with ``....Contd from previous page`` on the next line. The
  PTR / AUM blocks only live on the **first** page of each scheme; the
  continuation pages carry the rest of the holdings table.

  We accept BOTH first and continuation pages as scheme pages (the
  orchestrator dedupes by ``(scheme_code, as_of_month)`` so emitting twice
  is harmless — but we only yield a record from the page that actually
  carries the value).

* PTR is printed as a **percent**: ``Equity Turnover 9.14%``. Storage uses
  a fraction (``9.14% → 0.0914``). HDFC also prints ``Total Turnover`` (the
  sum of equity + debt + derivative turnover); we use ``Equity Turnover``
  as the canonical Phase-2 metric because for hybrid / multi-asset funds
  the equity turnover is what drives the trade-cost component.

* AUM is printed as ``As on April 30, 2026 ₹100,479.23Cr.`` (Month-End
  AUM). ``Average for Month`` (AAUM) prints the rolling average and is
  ignored.

* Two-strategy extraction for both PTR and AUM:
    1. **Text regex** (fast path) — matches the clean ``extract_text``
       output on most equity / large-cap / multi-cap pages.
    2. **Word-position fallback** — when ``extract_text`` column-bleeds the
       PTR or AUM line (e.g. p11 Large Cap, p28 Transportation, p32 Pharma,
       p33 Housing, p35 Infrastructure, p52 Equity Savings, p78 Money
       Market, p82 Medium Term Debt, p92 Corporate Bond), find the anchor
       words (``Equity`` + ``Turnover`` for PTR; ``As`` + ``on`` for AUM)
       in the left column and pick the next number-formatted token on the
       same y-band.

  Without the fallback the adapter writes ~24 PTR / ~47 AUM raw records.
  With it we get ~29 PTR / ~53 AUM raw records on this PDF, which after
  scheme-name fuzzy matching covers ≥22 PTR / ≥40 AUM ranked funds.

* Pages 110, 112, 114, 119, 120 are NAV summary / ETF index aggregation
  pages where TWO unrelated scheme names are concatenated on the first
  line (e.g. ``HDFC NIFTY GROWTH SECTORS 15 ETF NAV as at April 30, 2026
  ₹114.108 HDFC NIFTY 50 ETF NAV as at April 30, 2026 ₹268.6567``). They
  don't carry per-scheme PTR / AUM blocks — only NAV values — so we drop
  them by detecting either the ``NAV as at`` token in the header or two
  ``HDFC `` occurrences on the same line.

* Holdings extraction is preserved from Phase 2.2 (two-column equity
  layout, no ISIN — the Excel-based Phase 3.C adapter at
  ``mfs.ingest.holdings.hdfc`` is the canonical ISIN-tagged source).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pdfplumber

from mfs.ingest._common import MONTH_NAMES, parse_ptr_pages, publish_ym
from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _data_month_name(data_ym: str) -> str:
    """data month YYYY-MM → MonthName (e.g. 'April')."""
    _, m = data_ym.split("-")
    return MONTH_NAMES[int(m) - 1]


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from the first non-empty line of a
    scheme page, or ``None`` for non-scheme pages (cover, TOC, market
    commentary, TER / NAV aggregation, disclosures).

    A scheme page's first non-empty line is ``HDFC <name>``. NAV aggregation
    pages (e.g. p110, p112, p119) concatenate two unrelated scheme banners
    on the same line and contain the substring ``NAV as at`` — we drop them.
    Continuation pages (``....Contd from previous page``) ARE accepted; the
    name on those is identical to the first page so it deduplicates via the
    PK (scheme_code, as_of_month).
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.startswith("HDFC "):
        return None
    # NAV aggregation page (multi-scheme banner): drop.
    if "NAV as at" in first or "NAV a s at" in first:
        return None
    # Multi-scheme banner where two HDFC names share one line.
    if first.upper().count("HDFC ") > 1:
        return None
    # Strip trailing 'CATEGORY OF SCHEME' bleed (rare).
    first = re.split(r"\s+CATEGORY OF SCHEME", first, maxsplit=1)[0]
    # Collapse whitespace.
    first = re.sub(r"\s+", " ", first).strip()
    return first or None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# HDFC prints PTR as percent: "Equity Turnover 9.14%". Storage is a fraction
# (9.14% -> 0.0914), so we divide by 100 after extraction.
_PTR_TEXT_RE = re.compile(
    r"Equity\s+Turnover\s+(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)


def _extract_ptr_text(text: str) -> float | None:
    """Strategy 1 (fast path): text regex. Returns a FRACTION (9.14% → 0.0914)
    per the parse_ptr_pages unit convention, or None on miss."""
    m = _PTR_TEXT_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1)) / 100.0
    except ValueError:
        return None


def _ptr_word_fallback(page, text: str) -> float | None:
    """Strategy 2: word-position fallback when the label is present on the
    page but extract_text didn't capture the value cleanly (column bleed).

    Relaxed gating: pages where extract_text mangles the PTR line still
    typically retain the word 'Turnover' somewhere. ``_find_ptr_via_words``
    bails quickly when the left-column 'Equity'/'Turnover' anchor isn't
    present (e.g. on debt pages that legitimately omit the block) so the
    cost is minimal. Returns a fraction (percent / 100) or None.
    """
    if "Turnover" not in text:
        return None
    val = _find_ptr_via_words(page)
    if val is None:
        return None
    return val / 100.0


def _find_ptr_via_words(page) -> float | None:
    """Word-position fallback for PTR extraction.

    Locates the ``Equity`` + ``Turnover`` word pair in the left column
    (x0 < 200) on the same y-band, then takes the next numeric token on
    the same band (with or without a trailing ``%``). Used when the
    holdings table on the right of the page bleeds into the PTR line in
    ``extract_text`` output (observed on p11, p28, p32, p33, p35, p52).
    """
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None
    for i, w in enumerate(words):
        if w["text"] != "Equity":
            continue
        # Left column only — the body has its own 'Equity' tokens scattered
        # (e.g. "EQUITY & EQUITY RELATED" section banner).
        if w["x0"] >= 200:
            continue
        turnover: dict | None = None
        for j in range(i + 1, min(i + 6, len(words))):
            v = words[j]
            if abs(v["bottom"] - w["bottom"]) > 3:
                continue
            if v["text"].lower().startswith("turnover"):
                turnover = v
                break
        if turnover is None:
            continue
        for n in words:
            if abs(n["bottom"] - turnover["bottom"]) > 5:
                continue
            if n["x0"] <= turnover["x1"]:
                continue
            txt = n["text"].rstrip("%").rstrip(",;.")
            if re.fullmatch(r"\d+\.\d{1,3}", txt):
                try:
                    val = float(txt)
                except ValueError:
                    continue
                if val <= 0:
                    return None
                return val
    return None


# ---------------------------------------------------------------------------
# AUM extraction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@register_adapter
class HdfcAdapter(ManagerAdapter):
    """HDFC Mutual Fund factsheet adapter."""

    amc_slug = "hdfc"
    source_label = "HDFC Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Build the canonical PDF URL for data month ym='YYYY-MM'.

        HDFC publishes one calendar month after the data month, under
        ``files.hdfcfund.com/s3fs-public/<publish_ym>/`` with a filename
        encoding the data month name + data year.
        """
        publish = publish_ym(ym)
        month_name = _data_month_name(ym)
        data_year = ym.split("-")[0]
        return (
            f"https://files.hdfcfund.com/s3fs-public/{publish}/"
            f"HDFC%20MF%20Factsheet%20-%20{month_name}%20{data_year}.pdf"
        )

    # -------------------------------------------------------------------
    # PTR / AUM
    # -------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        # Both extractors convert HDFC's printed percent to the fraction the
        # parse_ptr_pages unit convention requires (9.14% → 0.0914).
        return parse_ptr_pages(
            pdf_path,
            scheme_name_fn=_scheme_name_from_page,
            ptr_extract_fn=_extract_ptr_text,
            amc_slug=self.amc_slug,
            page_fallback_fn=_ptr_word_fallback,
        )

    # ------------------------------------------------------------------
    # Holdings extraction (preserved from Phase 2.2.C)
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        # PDF-parser log noise is silenced at mfs.ingest._common import time.
        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                yield from self._parse_page_holdings(page, page_idx + 1)

    # HDFC portfolio pages are two-column: left ~x=195-365, right ~x=378-547.
    # Each row carries name | industry | weight in each column.
    _LEFT_NAME_RANGE = (190.0, 270.0)
    _LEFT_WEIGHT_RANGE = (340.0, 370.0)
    _RIGHT_NAME_RANGE = (375.0, 462.0)
    _RIGHT_WEIGHT_RANGE = (525.0, 555.0)

    # Section banners. Detection words must appear *together* in one y-band.
    _EQUITY_BANNER = ("EQUITY", "RELATED")  # full banner: "EQUITY & EQUITY RELATED"
    _STOP_TOKENS = {
        "Sub", "UNITS", "DEBT", "Cash", "Cash,", "Grand", "Treasury",
        "Government", "Mutual", "Convertible",
    }

    def _parse_page_holdings(self, page, page_num: int) -> Iterable[ParsedHoldingRecord]:
        text = page.extract_text() or ""
        scheme_name = _scheme_name_from_page(text)
        if not scheme_name:
            return
        try:
            words = page.extract_words(use_text_flow=True)
        except Exception:  # noqa: BLE001
            return

        eq_start_y = self._find_equity_banner_y(words)
        if eq_start_y is None:
            return

        # Group the equity-section words into y-bands ("rows" in the visual sense),
        # then walk top-to-bottom assembling holdings. Names may wrap across 2-3
        # consecutive y-bands; we accumulate name words in each column until we
        # hit a row that has a weight, which completes the holding.
        bands = self._equity_y_bands(words, eq_start_y)
        for hold in self._assemble_holdings(bands):
            yield ParsedHoldingRecord(
                scheme_name_printed=scheme_name,
                security_name=hold["name"],
                weight_pct=hold["weight"],
                isin=None,  # HDFC factsheets don't print ISINs
                instrument_type="Equity",
                source_amc=self.amc_slug,
            )

    @staticmethod
    def _find_equity_banner_y(words: list[dict]) -> float | None:
        """Return the y-position of the 'EQUITY & EQUITY RELATED' header row,
        or None if not present on this page."""
        for w in words:
            if w["text"] != "EQUITY":
                continue
            # Look for another 'EQUITY' on the same y, slightly to the right.
            for v in words:
                if (
                    v is not w
                    and v["text"] == "EQUITY"
                    and abs(v["bottom"] - w["bottom"]) < 3
                    and v["x0"] > w["x1"]
                ):
                    # And a 'RELATED' word immediately after
                    for r in words:
                        if (
                            r["text"] == "RELATED"
                            and abs(r["bottom"] - v["bottom"]) < 3
                            and r["x0"] > v["x1"]
                        ):
                            return w["bottom"]
        return None

    @classmethod
    def _equity_y_bands(
        cls, words: list[dict], eq_start_y: float
    ) -> list[list[dict]]:
        """Group portfolio-area words into visual rows (y-bands).

        Stops at the first y-band containing a stop token in either column —
        that's where the EQUITY section ends and Sub Total / DEBT / Cash begins.
        """
        relevant = [
            w for w in words
            if w["bottom"] > eq_start_y + 2
            and cls._LEFT_NAME_RANGE[0] <= w["x0"] <= cls._RIGHT_WEIGHT_RANGE[1]
        ]
        relevant.sort(key=lambda w: (w["bottom"], w["x0"]))
        rows: list[list[dict]] = []
        for w in relevant:
            if rows and abs(w["bottom"] - rows[-1][-1]["bottom"]) < 3:
                rows[-1].append(w)
            else:
                rows.append([w])
        kept: list[list[dict]] = []
        for row in rows:
            tokens = {w["text"] for w in row}
            if tokens & cls._STOP_TOKENS:
                break
            kept.append(row)
        return kept

    @classmethod
    def _assemble_holdings(cls, bands: list[list[dict]]) -> Iterable[dict]:
        """Walk y-bands top-to-bottom, assembling multi-line names per column.

        For each column (left, right) we maintain a buffer of name words.
        When a band has a numeric weight in that column, the buffered name
        plus the band's name-column words form a complete holding. If a band
        has name words but NO weight in that column, the words go into the
        buffer (it's a name-continuation row).
        """
        # Buffers: list of (x_anchor, word_text) tuples to preserve column order.
        left_buffer: list[dict] = []
        right_buffer: list[dict] = []

        for band in bands:
            left_name = [
                w for w in band
                if cls._LEFT_NAME_RANGE[0] <= w["x0"] <= cls._LEFT_NAME_RANGE[1]
                and w["text"] not in {"•", "@"}
            ]
            right_name = [
                w for w in band
                if cls._RIGHT_NAME_RANGE[0] <= w["x0"] <= cls._RIGHT_NAME_RANGE[1]
                and w["text"] not in {"•", "@"}
            ]
            left_weight = cls._find_weight(band, cls._LEFT_WEIGHT_RANGE)
            right_weight = cls._find_weight(band, cls._RIGHT_WEIGHT_RANGE)

            # Left column
            left_buffer.extend(left_name)
            if left_weight is not None:
                name = cls._assemble_name(left_buffer)
                if name:
                    yield {"name": name, "weight": left_weight}
                left_buffer = []

            # Right column (independent)
            right_buffer.extend(right_name)
            if right_weight is not None:
                name = cls._assemble_name(right_buffer)
                if name:
                    yield {"name": name, "weight": right_weight}
                right_buffer = []

    @staticmethod
    def _find_weight(
        band: list[dict], weight_range: tuple[float, float]
    ) -> float | None:
        """Return the numeric value in the weight column of this band, if any."""
        candidates = [
            w for w in band
            if weight_range[0] <= w["x0"] <= weight_range[1]
        ]
        for w in reversed(candidates):
            t = w["text"].replace(",", "")
            try:
                return float(t)
            except ValueError:
                continue
        return None

    @staticmethod
    def _assemble_name(buffer: list[dict]) -> str:
        """Concatenate buffered name words in (y, x) order with footnote cleanup."""
        if not buffer:
            return ""
        ordered = sorted(buffer, key=lambda w: (round(w["bottom"]), w["x0"]))
        name = " ".join(w["text"] for w in ordered).strip()
        name = re.sub(r"[£@†‡#*]", "", name)
        name = re.sub(r"\s+", " ", name).strip()
        return name
