"""DSP Mutual Fund — factsheet adapter (Phase 3).

Calibrated against the April 2026 combined factsheet at
``data/raw/factsheets/dsp/2026-04.pdf`` (~13.6 MB, 167 pages, 84 scheme
pages followed by IDCW history / TER / disclosure pages). PDF discovered
at the canonical URL

    https://www.dspim.com/latest-literature/dsp-factsheet-april-2026.pdf

which 302-redirects to ``media/pages/docs/...``. The pattern
``dsp-factsheet-<monthname>-<YYYY>.pdf`` is stable month-over-month (the
URL was probed by HEAD-checking ``april/march/february`` 2026 candidates).

Layout findings driving the parser:

* Each scheme page's first non-empty line starts with ``"DSP "`` followed
  by the scheme display name. Names occasionally wrap to a second line
  for the long FoF titles like ``DSP World Mining Overseas Equity Omni
  FoF (Erstwhile`` -> wrapped continuation. We strip any trailing
  ``(Erstwhile ... )`` annotation so the printed name stays canonical.

* PTR is printed in a vertical block in the **left column** as three
  separate visual rows:

      ``Portfolio Turnover Ratio``
      ``(Last 12 months):``
      ``0.29``

  Plain ``extract_text`` interleaves these lines with right-column
  portfolio holdings, so the numeric value lands several lines below the
  label. We use word-position extraction: find the ``Portfolio Turnover
  Ratio`` anchor, then take the first ``\\d+\\.\\d+`` token below it in
  the same x-column. PTR is **already a fraction** (DSP Flexi Cap prints
  ``0.29``, i.e. 29%), so no unit conversion.

* 84 scheme pages total: 52 carry PTR (equity / hybrid / arbitrage /
  most ETFs / index funds). Pure debt / gold-silver ETF / some FoF pages
  legitimately don't print PTR; the orchestrator handles asymmetric
  coverage.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pdfplumber

from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)


_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


_FRACTION_RE = re.compile(r"^\d+\.\d+$")
# Trailing erstwhile/parenthetical descriptions that bleed onto line 1.
_ERSTWHILE_RE = re.compile(r"\s*\(Erstwhile[^)]*$", re.IGNORECASE)
_PAREN_TAIL_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _normalize_scheme_name(name: str) -> str:
    """Disambiguate the one DSP printed name that collides under the matcher.

    The factsheet prints the active midcap fund as ``'DSP Mid Cap Fund'``
    (spaced). That token-set-ties at 100 against ``'DSP Large & Mid Cap
    Fund'`` and loses the Levenshtein tie-break, so its PTR mis-routes to
    Large & Mid Cap and DSP Midcap Fund silently gets no PTR. scheme_master
    spells the fund ``'DSP Midcap Fund'`` (no space), so we collapse exactly
    that printed name. Anchored on the full (case-insensitive) string, so
    the ``'DSP Large & Mid Cap Fund'`` page is left untouched.
    """
    if name.strip().lower() == "dsp mid cap fund":
        return "DSP Midcap Fund"
    return name


@register_adapter
class DspAdapter(ManagerAdapter):
    """DSP Mutual Fund factsheet adapter.

    ``amc_slug`` matches ``scheme_master.amc_code`` directly (``dsp``), so
    no alias entry is needed in ``_scheme_match.py``.
    """

    amc_slug = "dsp"
    source_label = "DSP Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical DSP combined-factsheet PDF URL.

        DSP names the file with the **data month** (April 2026 publish has
        ``april-2026`` in the path; no separate publish-month folder).
        The ``/latest-literature/`` path 302-redirects to the actual
        storage location under ``/media/pages/docs/``.
        """
        y, m = map(int, ym.split("-"))
        month_lower = _MONTH_NAMES[m - 1]
        return (
            "https://www.dspim.com/latest-literature/"
            f"dsp-factsheet-{month_lower}-{y}.pdf"
        )

    # ------------------------------------------------------------------
    # Scheme-name extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _scheme_name_from_page(text: str) -> str | None:
        """Return the printed scheme name from a scheme page, or None.

        Scheme pages start with a first non-empty line ``DSP <name>``.
        Cover, TOC, and disclosure pages start with something else.
        Trailing ``(Erstwhile known as ...)`` annotations are stripped
        so the canonical form matches scheme_master entries.
        """
        if not text:
            return None
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return None
        first = lines[0]
        if not first.startswith("DSP "):
            return None
        # Strip footnote markers DSP appends to a few scheme names
        # ('DSP ELSS Tax Saver Fund$$') BEFORE paren handling so the
        # paren-tail regex anchors correctly.
        first = re.sub(r"\${1,2}$", "", first).strip()
        # Strip a wrapped '(Erstwhile ...' fragment (no closing paren on
        # line 1 — the close lands on line 2).
        first = _ERSTWHILE_RE.sub("", first).strip()
        # Strip a fully-balanced '(Erstwhile ...)' parenthetical.
        first = _PAREN_TAIL_RE.sub("", first).strip()
        if not first:
            return None
        # Disambiguate 'DSP Mid Cap Fund' from 'DSP Large & Mid Cap Fund'
        # before the fuzzy matcher sees it (see _normalize_scheme_name).
        return _normalize_scheme_name(first)

    # ------------------------------------------------------------------
    # PTR
    # ------------------------------------------------------------------

    @staticmethod
    def _ptr_from_words(words: list[dict]) -> float | None:
        """Locate ``Portfolio Turnover Ratio`` label, then the first
        numeric token below it in the same x-column.

        DSP renders PTR as three stacked rows in the left rail (or, on a
        minority of pages, the right rail). We find the *adjacent*
        ``Portfolio`` + ``Turnover`` + ``Ratio`` words to anchor the
        label, then scan downward in a ±20pt x-window for the first
        ``\\d+\\.\\d+`` token. The ``(Last 12 months):`` row is
        deliberately skipped because it carries no number.
        """
        anchor = None
        for w in words:
            if w["text"] != "Portfolio":
                continue
            # 'Turnover' immediately to the right on the same y-band.
            for v in words:
                if v["text"] != "Turnover":
                    continue
                if abs(v["bottom"] - w["bottom"]) > 3:
                    continue
                if not (w["x1"] < v["x0"] < w["x1"] + 30):
                    continue
                anchor = w  # use the 'Portfolio' word as the anchor
                break
            if anchor is not None:
                break
        if anchor is None:
            return None
        anchor_x = anchor["x0"]
        anchor_y = anchor["bottom"]
        # Search a vertical window below the anchor for fractional values
        # in the same column. The window must extend far enough to clear
        # the '(Last 12 months):' caption (~9-12pt below the label).
        candidates = []
        for n in words:
            if n["bottom"] < anchor_y + 5:
                continue
            if n["bottom"] > anchor_y + 50:
                continue
            if abs(n["x0"] - anchor_x) > 25:
                continue
            if _FRACTION_RE.fullmatch(n["text"]):
                candidates.append(n)
        if not candidates:
            return None
        candidates.sort(key=lambda c: c["bottom"])
        try:
            return float(candidates[0]["text"])
        except ValueError:
            return None

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = self._scheme_name_from_page(text)
                if not scheme:
                    continue
                if "Portfolio Turnover" not in text:
                    continue
                try:
                    words = page.extract_words(use_text_flow=False)
                except Exception:  # noqa: BLE001
                    continue
                ptr = self._ptr_from_words(words)
                if ptr is None:
                    continue
                # Fail-fast: drop NaN / non-positive values rather than
                # ship garbage. PTR == 0 is implausible for an active
                # equity scheme; if it happens we'd rather have no row.
                if ptr != ptr or ptr <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr,
                    source_amc=self.amc_slug,
                )

    # ------------------------------------------------------------------
    # Holdings (deferred — Phase 3.C provides the ISIN-tagged Excel path).
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
