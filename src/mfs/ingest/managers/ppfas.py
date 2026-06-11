"""PPFAS (Parag Parikh) Mutual Fund — factsheet PTR adapter.

Calibrated against the April 2026 combined factsheet PDF
(``data/raw/factsheets/ppfas/2026-04.pdf``, ~3.9 MB, 20 pages).

URL pattern (discovered via WebSearch on amc.ppfas.com):
  https://amc.ppfas.com/downloads/factsheet/{data_year}/
    ppfas-mf-factsheet-for-{MonthName}-{data_year}.pdf
The filename encodes the DATA month name + DATA year (NOT the publish
month), and lives in a sub-folder named after the DATA year. PPFAS
publishes ~8 days into the following month, but the path keys off the
data month, so no publish-month shift is needed (unlike HDFC). The live
URL on the site also carries a ``?DDMMYYYY=`` cache-buster query string;
it is optional and we omit it (server returns 200 without it).

Layout findings driving the parser
----------------------------------
* PPFAS combines all schemes into one small PDF. Only the per-scheme
  fact pages carry a Portfolio Turnover line, and only for funds with a
  meaningful equity book and >=1yr history. In 2026-04 exactly two
  schemes print it:
      - Parag Parikh Flexi Cap Fund (p2)
      - Parag Parikh ELSS Tax Saver Fund (p5)
  The other schemes legitimately OMIT it:
      - Large Cap (p7): allotted Feb-2026, <1yr old, no PTR yet.
      - Dynamic Asset Allocation (p8), Conservative Hybrid (p11),
        Arbitrage (p14), Liquid (p16): hybrid / arbitrage / debt funds
        for which PPFAS does not print a portfolio turnover ratio.
  We do NOT fabricate values for those — half data is worse than none.

* PTR is printed as a **percent**, e.g. ``Portfolio Turnover 22.48%``.
  Storage uses a fraction, so we divide by 100 (22.48% -> 0.2248).

* The Flexi Cap page prints TWO turnover figures:
      ``Portfolio Turnover (excl Equity Arbitrage) 17.91%``
      ``Portfolio Turnover (incl Equity Arbitrage) 44.46%``
  We take the **excl Equity Arbitrage** value as the canonical PTR. It
  reflects the genuine equity-book turnover; the ``incl`` figure is
  inflated by high-frequency arbitrage roll-overs, mirroring why the
  HDFC/SBI adapters use ``Equity Turnover`` rather than ``Total
  Turnover``. The ELSS page prints a single bare ``Portfolio Turnover``.

* Scheme-name anchor: the noisy first page line column-bleeds on several
  pages (e.g. p8/p9 render as "Parag Parikh This Scheme ..."). The
  reliable anchor present on every scheme page is the
  ``Name of the Fund   Parag Parikh <Name> (PPxxx)`` row. We extract the
  name from there and strip the parenthetical ticker.

* Page 20 is the glossary; it contains the words "Portfolio Turnover
  Ratio" inside a definition paragraph with no scheme anchor, so it is
  naturally skipped (no ``Name of the Fund`` line).

Holdings extraction is left at the base-class default — PPFAS holdings
come from the separate Excel path, not this factsheet PDF.

Calibration on 2026-04: 2 PTR records (Flexi Cap 0.1791, ELSS 0.2248).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pdfplumber

from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------

# Every scheme page prints "Name of the Fund   Parag Parikh <Name> (PPxxx)".
# This survives the column-bleed that mangles the visual page header.
_SCHEME_NAME_RE = re.compile(
    r"Name of the Fund\s+(Parag Parikh .+?)\s*\(PP[A-Z]+\)",
)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from the 'Name of the Fund' row, or
    ``None`` for non-scheme pages (cover, charts-only continuation pages,
    glossary, disclosures)."""
    if not text:
        return None
    m = _SCHEME_NAME_RE.search(text)
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group(1)).strip()
    return name or None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# PPFAS prints PTR as a PERCENT, e.g. "Portfolio Turnover 22.48%". Storage is
# a fraction (22.48% -> 0.2248), so we divide by 100 after extraction.
#
# Flexi Cap prints two variants; we prefer "(excl Equity Arbitrage)" — the
# genuine equity-book turnover — over "(incl Equity Arbitrage)" which is
# inflated by arbitrage roll-overs. The "excl" pattern is tried first; the
# bare-label pattern (no parenthetical) is the fallback used by ELSS.
_PTR_EXCL_RE = re.compile(
    r"Portfolio\s+Turnover\s*\(\s*excl[^)]*\)\s*(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_PTR_BARE_RE = re.compile(
    r"Portfolio\s+Turnover\s+(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)


def _find_ptr_percent(text: str) -> float | None:
    """Return the PTR as a percent value (not yet divided by 100), or None.

    Prefer the 'excl Equity Arbitrage' figure when present; otherwise the
    bare 'Portfolio Turnover NN.NN%' line.
    """
    m = _PTR_EXCL_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = _PTR_BARE_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@register_adapter
class PpfasAdapter(ManagerAdapter):
    """PPFAS (Parag Parikh) Mutual Fund factsheet adapter."""

    amc_slug = "ppfas"
    source_label = "Parag Parikh Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Build the canonical PDF URL for data month ym='YYYY-MM'.

        PPFAS keys the path off the DATA month (filename + year sub-folder),
        not the publish month, so no forward shift is applied.
        """
        y, m = map(int, ym.split("-"))
        month_name = _MONTH_NAMES[m - 1]
        return (
            f"https://amc.ppfas.com/downloads/factsheet/{y}/"
            f"ppfas-mf-factsheet-for-{month_name}-{y}.pdf"
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
                pct = _find_ptr_percent(text)
                if pct is None:
                    continue
                # Drop NaN / non-positive — PPFAS prints a real percent or
                # omits the block; there is no zero-PTR scheme.
                if pct != pct or pct <= 0:
                    continue
                # Convert percent -> fraction.
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=pct / 100.0,
                    source_amc=self.amc_slug,
                )
