"""JM Financial Mutual Fund — factsheet PTR adapter.

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/jm_financial/2026-04.pdf``, ~8.5 MB, 57 pages,
"Details as on April 30, 2026").

URL pattern
-----------
JM publishes the combined factsheet under a CMS path and names the file by
the **publish month**, which is one calendar month AFTER the data month
(same shift as HDFC). The data-month=2026-04 file (data as on
30-Apr-2026) is therefore published as ``Factsheet May 2026.pdf``::

    https://www.jmfinancialmf.com/CMS/downloads/Factsheet/Factsheet/
        Factsheet%20<PublishMonthName>%20<PublishYear>.pdf

Verified live: ``Factsheet May 2026.pdf`` → "Details as on April 30, 2026";
``Factsheet April 2026.pdf`` → "Details as on March 31, 2026". So
``build_url`` shifts the data month forward by one before formatting.

Layout findings driving the parser
----------------------------------
* Each scheme occupies its own page (pages ~22-40). The printed scheme
  name is the **first non-empty line** of the page, prefixed ``JM `` (e.g.
  ``JM Flexicap Fund``). Continuation pages repeat the same first line but
  do NOT carry the Quantitative-data block (no PTR), so they naturally
  produce no record. The cover page (``JM Financial``) and the
  category-index pages (``JM EQUITY SCHEMES`` / ``JM DEBT SCHEMES``) also
  carry no PTR value and are dropped.

* PTR is labelled ``PORTFOLIO TURNOVER RATIO`` and printed as a **fraction**
  already (e.g. ``PORTFOLIO TURNOVER RATIO 0.7674`` = 76.74% turnover).
  Storage uses the fraction convention, so we PASS THROUGH with NO division
  by 100. (Arbitrage funds legitimately print large fractions like 10.72 —
  ~1072% turnover — which is normal, not a unit error.)

* Young funds that have not completed one year print the prose
  ``Portfolio Turnover Ratio is not computed since the Scheme has not ...``
  with NO trailing number (observed on the JM Large & Mid Cap Fund page).
  The regex requires a numeric token immediately after the label, so these
  pages yield no record (fail-fast: drop rather than fabricate).

* Page 21 contains the false-positive phrase "JOLTS Job Openings and Labor
  Turnover Survey" in macro commentary. Anchoring on the full
  ``PORTFOLIO TURNOVER RATIO`` label (not the bare word "Turnover") avoids
  it; that page also has no ``JM `` first line so it is double-gated out.

PTR rows extracted from 2026-04: 9
  ELSS Tax Saver, Flexicap, Midcap, Small Cap, Large Cap, Value, Focused,
  Arbitrage, Aggressive Hybrid.

Holdings extraction is left as the base-class default — JM holdings come
from the separate Excel path (Phase 3.C), not this PDF.
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


def _publish_ym(data_ym: str) -> tuple[str, int]:
    """data month 'YYYY-MM' → (publish_month_name, publish_year).

    JM names the file by the publish month, which is data month + 1
    (with year rollover for December data).
    """
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return _MONTH_NAMES[0], y + 1
    return _MONTH_NAMES[m], y  # _MONTH_NAMES[m] is month m+1 (0-indexed list)


# JM prints PTR as a FRACTION already: "PORTFOLIO TURNOVER RATIO 0.7674"
# (= 76.74% turnover). Storage is a fraction, so we PASS THROUGH — NO /100.
# Requiring a numeric token right after the label drops young funds that
# print "Portfolio Turnover Ratio is not computed ..." with no number.
_PTR_RE = re.compile(
    r"PORTFOLIO\s+TURNOVER\s+RATIO\s+(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name (first non-empty ``JM `` line) or None.

    Drops the cover page (``JM Financial``) and the category-index pages
    (``JM EQUITY SCHEMES`` / ``JM DEBT SCHEMES``) which carry no per-scheme
    PTR block anyway; the all-caps token after ``JM `` distinguishes those
    banners from real scheme titles like ``JM Flexicap Fund``.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = re.sub(r"\s+", " ", lines[0]).strip()
    if not first.startswith("JM "):
        return None
    if first == "JM Financial":
        return None
    # Category-index banners are fully upper-cased ("JM EQUITY SCHEMES").
    rest = first[len("JM "):]
    if rest.isupper():
        return None
    return first or None


@register_adapter
class JmFinancialAdapter(ManagerAdapter):
    """JM Financial Mutual Fund factsheet adapter (PTR only)."""

    amc_slug = "jm_financial"
    source_label = "JM Financial Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Build the canonical PDF URL for data month ym='YYYY-MM'.

        JM publishes one calendar month after the data month and names the
        file by that publish month: data 2026-04 → ``Factsheet May 2026.pdf``.
        """
        month_name, year = _publish_ym(ym)
        return (
            "https://www.jmfinancialmf.com/CMS/downloads/Factsheet/Factsheet/"
            f"Factsheet%20{month_name}%20{year}.pdf"
        )

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging

        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                m = _PTR_RE.search(text)
                if not m:
                    continue
                try:
                    ptr_value = float(m.group(1))
                except ValueError:
                    continue
                # Drop NaN / non-positive — JM prints a real fraction or omits
                # the block entirely; there is no zero-PTR scheme.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                # Pass-through: already a fraction (NO division by 100).
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )
