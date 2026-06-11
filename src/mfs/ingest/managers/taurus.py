"""Taurus Mutual Fund — combined factsheet adapter (Phase 3 PTR coverage).

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/taurus/2026-04.pdf``, ~4.4 MB, 26 pages). Taurus
publishes one "Taurus Times" PDF per month covering every scheme; the
April 2026 data ("as on 30th April 2026") is published in May 2026.

URL pattern
-----------
``https://www.taurusmutualfund.com/sites/default/files/downloads/
   Taurus_Times_<MonthName>_<Year>.pdf``
The filename uses the FULL English month name of the DATA month
(``Taurus_Times_April_2026.pdf`` carries 30-Apr-2026 data) and the data
year. Confirmed live: April 2026 → 200/4.4 MB, March 2026 → 200/5.7 MB.
The data month == publish-month-minus-one in the date-as-on text, but the
filename itself encodes the DATA month, so ``build_url`` does NOT shift the
month forward (unlike HDFC). It only maps ``ym`` → month name + year.

Layout findings driving the parser
----------------------------------
* Each scheme occupies its own page (pages 11-18 in the April 2026 PDF).
  The printed scheme name is the **first non-empty line** of the page,
  e.g. ``TAURUS FLEXI CAP FUND`` / ``Taurus Mid Cap Fund``. Later lines
  carry the "(earlier known as ...)" parenthetical and the SEBI category,
  so we take ONLY the first line as the name anchor.

* PTR is printed as a **fraction**: ``Portfolio Turnover: 0.88`` (observed
  range 0.14-0.92 across schemes — sane equity turnover). Storage is the
  same fraction convention, so we PASS THROUGH without dividing by 100.
  pdfplumber renders the "ti"/"tti" ligature in "Portfolio" as a NUL char
  (``Por\x00olio``), but the word "Turnover" itself extracts cleanly, so we
  anchor the regex on ``Turnover:`` rather than the mangled "Portfolio".

* The PTR line frequently bleeds the right-hand sector/holdings column onto
  the same text line (e.g. ``Portfolio Turnover: 0.88 EQUITY SECTOR
  ALLOCATION MARKET CAPITALISATION``). The regex therefore captures only
  the first numeric token after ``Turnover:`` and stops.

* Page 1 carries a glossary that DEFINES "Portfolio Turnover Ratio" in
  prose ("...is the percentage of a fund's...") but prints no value and no
  ``Turnover:`` token, so it is naturally skipped.

* The 7 ranked equity funds (Flexi Cap, Ethical, Mid Cap, ELSS Tax Saver,
  Large Cap, Banking & Financial Services, Infrastructure) plus the Nifty
  50 Index Fund all carry PTR → 8 raw PTR records on the April 2026 PDF.

Slug
----
``amc_slug == scheme_master.amc_code == "taurus"`` — no alias needed in
``_scheme_match.py`` (verified: scheme_master has DIRECT + GROWTH rows
directly under amc_code "taurus").

Holdings are not extracted here (the Excel-based holdings path is canonical);
``parse_holdings`` is left as the base-class no-op.
"""

from __future__ import annotations

import logging
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

# Taurus prints PTR as a FRACTION: "Portfolio Turnover: 0.88" (range
# ~0.14-0.92). Storage is also a fraction, so PASS THROUGH (no /100).
# The "Portfolio" word ligature-mangles to "Por\x00olio" but "Turnover:"
# is clean; anchor on it and capture only the leading number (the holdings
# column often bleeds onto the same line).
_PTR_TEXT_RE = re.compile(r"Turnover\s*:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name (first non-empty line) for a Taurus
    scheme page, or ``None`` for non-scheme pages.

    Scheme pages begin with ``TAURUS <name>`` / ``Taurus <name>`` on the
    first non-empty line. We require that anchor and reject lines that are
    clearly not a scheme banner (glossary, disclaimer prose).
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.lower().startswith("taurus"):
        return None
    # Drop ligature NULs and collapse whitespace.
    first = first.replace("\x00", "ti")
    first = re.sub(r"\s+", " ", first).strip()
    # A scheme banner is a short title, not a sentence of glossary prose.
    if len(first) > 80 or first.endswith((".", ",")):
        return None
    return first or None


@register_adapter
class TaurusAdapter(ManagerAdapter):
    """Taurus Mutual Fund factsheet adapter (PTR only)."""

    amc_slug = "taurus"
    source_label = "Taurus Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Build the canonical 'Taurus Times' PDF URL for data month ym.

        Taurus names the file with the FULL data-month name + data year
        (e.g. 2026-04 → Taurus_Times_April_2026.pdf). No publish-month
        shift: the filename encodes the data month directly.
        """
        y, m = map(int, ym.split("-"))
        month_name = _MONTH_NAMES[m - 1]
        return (
            "https://www.taurusmutualfund.com/sites/default/files/downloads/"
            f"Taurus_Times_{month_name}_{y}.pdf"
        )

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        logging.getLogger("pdfminer").setLevel(logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                m = _PTR_TEXT_RE.search(text)
                if not m:
                    continue
                try:
                    ptr_value = float(m.group(1))
                except ValueError:
                    continue
                # Drop NaN / non-positive — Taurus prints a real fraction or
                # omits the block; there is no zero-PTR equity scheme.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,  # fraction, pass-through
                    source_amc=self.amc_slug,
                )
