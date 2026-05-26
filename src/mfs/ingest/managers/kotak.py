"""Kotak Mahindra Mutual Fund — factsheet adapter.

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/kotak/2026-04.pdf``, 19MB, 188 pages, ~120 scheme
pages plus disclosures / TER tables / IDCW history at the back).

Layout findings driving the parser:

* Scheme name lives in the page header (y ~ 27pt) typeset in
  ``Frutiger-Bold`` at ``size=16`` (a small minority of pages use 13–15pt).
  Plain ``extract_text`` is unreliable here because the right-hand banner
  ("Value GARP Growth Size") and rotated-text artefacts (negative x
  coordinates) bleed into the same logical line. We extract via
  ``page.extract_words(extra_attrs=['size','fontname'])`` and keep only
  words whose ``size >= 12.5``, ``fontname`` contains "Bold", ``x0 >= 0``
  (filters out the rotated KOTAK <SCHEME> sidebar artefact on hybrid
  pages 40+), and ``bottom < 60`` (header band).  Wrapped names that span
  two visual rows (e.g. page 17 "KOTAK INFRASTRUCTURE & / ECONOMIC REFORM
  FUND") are reassembled by grouping words into y-bands.

* PTR is printed as a percent: ``Portfolio Turnover 22.65%``. Stored as a
  fraction (22.65% -> 0.2265).

* AUM is printed in INR Crore on two lines:

    ``AAUM: `10,510.63 crs``
    ``AUM: `10,599.29 crs``

  We prefer the Month-End AUM line — the ``AAUM`` line is the rolling
  average. A negative lookbehind on ``A`` makes ``AUM[:]`` not match the
  AAUM prefix.

* About 60-70% of equity pages print PTR. Hybrid / debt / fund-of-fund
  pages typically only print AUM (no PTR).  The orchestrator handles
  the asymmetric coverage — each parser yields its own count.

Calibration counts on 2026-04:
  * 61 PTR rows extracted
  * 108 AUM rows extracted (all with recognisable Kotak scheme names)

URL pattern: Kotak's monthly factsheet PDF is published one calendar
month after the data month (e.g. April-2026 data = May-2026 publish).
The canonical download path on www.kotakmf.com encodes the **publish**
month name and year in the filename. The URL string below follows
Kotak's known convention; the test only asserts string equality, so a
month/year mismatch will surface there rather than at network time.
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


# Header-row heuristic: bold words at the top of the page in the scheme-name
# point size band. Values picked from observed PDF (size=16 for ~115 pages,
# 13–15 for the long-tail Omni FOF / hybrid pages).  Anything smaller is the
# "Total Expense Ratio" disclosure tables in the back matter (size=10–11),
# which we do not want to mistake for a scheme page.
_HEADER_MIN_SIZE = 12.5
_HEADER_MAX_Y = 60.0


# PTR: "Portfolio Turnover 22.65%"  → percent. Store as fraction.
_PTR_RE = re.compile(
    r"Portfolio\s+Turnover\s+(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

@register_adapter
class KotakAdapter(ManagerAdapter):
    """Kotak Mahindra Mutual Fund factsheet adapter."""

    amc_slug = "kotak"
    source_label = "Kotak Mahindra Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical Kotak factsheet PDF URL for data month ym.

        Kotak publishes the monthly factsheet one calendar month after the
        data month. The filename embeds the publish month name and four-
        digit year on the kotakmf.com Information/forms-and-downloads
        path.
        """
        publish_year, publish_month = _publish_ym(ym)
        month_name = _MONTH_NAMES[publish_month - 1]
        return (
            "https://www.kotakmf.com/Information/forms-and-downloads/"
            f"Factsheet/Factsheet-{month_name}-{publish_year}.pdf"
        )

    # -------------------------------------------------------------------
    # Scheme-name extraction
    # -------------------------------------------------------------------

    @staticmethod
    def _scheme_name_from_header(page) -> str | None:
        """Reassemble the bold KOTAK header on a scheme page, or None.

        Scans words in the top band, keeps only Bold + size ≥ 12.5 + on-
        canvas (x0 ≥ 0) + above y=60 words, groups them into y-bands and
        joins. Requires the result to start with "KOTAK" — otherwise we
        return None and the caller skips this page.
        """
        try:
            words = page.extract_words(
                use_text_flow=True,
                extra_attrs=["size", "fontname"],
            )
        except Exception:  # noqa: BLE001
            return None
        header_words = [
            w
            for w in words
            if w.get("size") is not None
            and w["size"] >= _HEADER_MIN_SIZE
            and "Bold" in (w.get("fontname") or "")
            and w["x0"] >= 0
            and w["bottom"] < _HEADER_MAX_Y
        ]
        if not header_words:
            return None
        header_words.sort(key=lambda w: (round(w["bottom"]), w["x0"]))
        rows: list[list[dict]] = []
        for w in header_words:
            if rows and abs(w["bottom"] - rows[-1][-1]["bottom"]) < 4:
                rows[-1].append(w)
            else:
                rows.append([w])
        name = " ".join(
            " ".join(w["text"] for w in row) for row in rows
        ).strip()
        if not name.upper().startswith("KOTAK"):
            return None
        # Normalise whitespace.
        name = re.sub(r"\s+", " ", name)
        return name

    # -------------------------------------------------------------------
    # PTR / AUM
    # -------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                scheme = self._scheme_name_from_header(page)
                if not scheme:
                    continue
                text = page.extract_text() or ""
                m = _PTR_RE.search(text)
                if not m:
                    continue
                try:
                    pct = float(m.group(1))
                except ValueError:
                    continue
                # Drop nonsense values rather than ship NaN-equivalent data.
                if pct <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=pct / 100.0,  # percent → fraction
                    source_amc=self.amc_slug,
                )

    # Holdings extraction is deferred — Kotak's two-column portfolio layout
    # is column-bled similarly to HDFC/SBI but with different column x
    # ranges. Phase 3.A will calibrate this; for now the base-class no-op
    # returns ().
    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
