"""Shriram Mutual Fund — factsheet adapter (Phase 3 PTR coverage).

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/shriram/2026-04.pdf``, ~1.4 MB, 20 pages, data as on
30-Apr-2026).

URL pattern
-----------
Shriram serves the combined monthly factsheet from a CDN, organized by the
Indian **financial year** folder (Apr-Mar), with the data-month name + data
year in the filename::

    https://cdn.shriramamc.in/uploads/fact-sheet/Fact-Sheet/<fy>/
        SAMC-Factsheet-<Month>-<Year>.pdf   (current, FY 2026-2027)

The financial-year folder for data month YYYY-MM is:
  - ``YYYY-(YYYY+1)`` for months Apr..Dec (m >= 4)
  - ``(YYYY-1)-YYYY`` for Jan..Mar (m <= 3)

So April 2026 lives under ``2026-2027/`` while March 2026 lives under
``2025-2026/``. ``build_url(ym)`` encodes this so next month's URL is
produced too.

Caveat (layout-stable as of Apr-2026, watch for drift): the *current*
April-2026 file is named ``SAMC-Factsheet-April-2026.pdf`` (no ``(Full)``
infix and the full month name ``April``), whereas the prior FY's files used
``SAMC-Factsheet-(Full)-<AbbrevMonth>-<Year>.pdf`` (e.g. ``Mar``, ``Sept``,
``July``). We build the current ``SAMC-Factsheet-<FullMonth>-<Year>.pdf``
form; if a future month reverts to the ``(Full)`` infix the URL must be
updated. ``fetch()`` re-uses the cached PDF unconditionally so a calibration
run never re-downloads.

Layout findings driving the parser
----------------------------------
* Each scheme has a single page (no continuation pages). The header is three
  lines: ``SHRIRAM`` (banner), then the scheme name in UPPERCASE without the
  ``SHRIRAM`` prefix (e.g. ``MULTI SECTOR ROTATION FUND``), then a
  ``(<short name>) As on April 30, 2026`` line. We reconstruct the printed
  scheme name as ``Shriram <line-2 title>`` — the orchestrator's fuzzy
  matcher (uppercase + punctuation-strip + token_set_ratio) maps that to the
  ``Shriram ... Fund - Direct Plan Growth Option`` rows in scheme_master.

* PTR is printed as a **percent**:
  ``Annual Portfolio Turnover Ratio (Equity): 364.3%``. Storage is a fraction
  (364.3% -> 3.643), so we divide by 100 after extraction. The label is
  unambiguous and lives on its own row in the left stats column; on several
  pages ``extract_text`` bleeds holdings text onto the same logical line
  (e.g. ``... 364.3% R R Kabel Ltd. 2.53``) but the anchored regex still
  captures the value cleanly because the percent token immediately follows
  the colon.

* Only the six equity / hybrid schemes print this metric (Multi Sector
  Rotation, Flexi Cap, ELSS Tax Saver, Multi Asset Allocation, Aggressive
  Hybrid, Balanced Advantage). Debt / liquid / overnight / money-market
  schemes omit it — those rows are simply absent, which is correct
  (PTR is meaningless for them).

* Holdings extraction is left as the base-class default (the Excel-based
  holdings path is canonical for ISIN-tagged constituents).

Calibration counts on 2026-04
-----------------------------
* 6 PTR records parsed end-to-end (all six equity/hybrid schemes), values
  0.87-3.64 as fractions.
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
# URL helpers
# ---------------------------------------------------------------------------


def _financial_year_folder(ym: str) -> str:
    """Indian financial-year folder for data month YYYY-MM.

    FY runs Apr..Mar. April 2026 (m=4) -> '2026-2027'; March 2026 (m=3) ->
    '2025-2026'.
    """
    y, m = map(int, ym.split("-"))
    if m >= 4:
        return f"{y}-{y + 1}"
    return f"{y - 1}-{y}"


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------


def _scheme_name_from_page(text: str) -> str | None:
    """Reconstruct the printed scheme name from a Shriram scheme page.

    Scheme pages open with ``SHRIRAM`` on the first non-empty line and the
    UPPERCASE fund name (without the ``SHRIRAM`` prefix) on the next line,
    ending in ``FUND``. We return ``Shriram <that name>``. Non-scheme pages
    (cover, market commentary, glossary, disclosures) return ``None``.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    if not lines[0].upper().startswith("SHRIRAM"):
        return None
    # The fund name is the next line(s) ending in FUND. Some pages split it
    # across the banner; the canonical case is a single uppercase line.
    for ln in lines[1:4]:
        if re.search(r"\bFUND\b", ln, re.IGNORECASE):
            name = re.sub(r"\s+", " ", ln).strip()
            # Guard against the "(short name) As on ..." line slipping in.
            if name.startswith("(") or "As on" in name:
                continue
            return f"Shriram {name}"
    return None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Shriram prints PTR as a PERCENT:
#   "Annual Portfolio Turnover Ratio (Equity): 364.3%"
# Storage is a fraction (364.3% -> 3.643), so we divide by 100 after match.
_PTR_RE = re.compile(
    r"Portfolio\s+Turnover\s+Ratio\s*\(Equity\)\s*:\s*"
    r"(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@register_adapter
class ShriramAdapter(ManagerAdapter):
    """Shriram Mutual Fund factsheet adapter."""

    amc_slug = "shriram"
    source_label = "Shriram Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Build the canonical PDF URL for data month ym='YYYY-MM'.

        Shriram organizes files by financial-year folder and encodes the
        data-month full name + data year in the filename.
        """
        y, m = map(int, ym.split("-"))
        fy = _financial_year_folder(ym)
        month_name = _MONTH_NAMES[m - 1]
        return (
            "https://cdn.shriramamc.in/uploads/fact-sheet/Fact-Sheet/"
            f"{fy}/SAMC-Factsheet-{month_name}-{y}.pdf"
        )

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                m = _PTR_RE.search(text)
                if not m:
                    continue
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    # PTR present but scheme identity lost — drop (fail-fast,
                    # no half-data).
                    continue
                try:
                    pct = float(m.group(1))
                except ValueError:
                    continue
                # Drop NaN / non-positive — Shriram prints a real percent or
                # omits the block; there is no zero-PTR scheme.
                if pct != pct or pct <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=pct / 100.0,  # percent -> fraction
                    source_amc=self.amc_slug,
                )
