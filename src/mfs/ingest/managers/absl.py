"""Aditya Birla Sun Life Mutual Fund — manager-tenure adapter.

Calibrated against the April 2026 factsheet (absl-factsheet_may-2026.pdf
~11MB, 263 pages — publish month May 2026, data month April 2026 per the
cover banner).

Layout (different again from HDFC/SBI/Nippon):
- Each scheme page heading is a title-case line "Aditya Birla Sun Life X Fund"
  (line 2 of the page, after a section banner like "Equity Funds").
- Manager block is text-flow in a narrow left column (x < ~175). Right column
  starts at x ≥ ~194, with a clean 60-point gap — easier column separation
  than SBI's ~30-pt gap.
- **Each manager has their OWN "Fund Manager" header** (unlike Nippon's single
  "Fund Manager(s)" header). Each block is:
      Fund Manager -Mr. <Name>
      Managing the Fund Since: <Month> <DD>, <YYYY>
      Experience in Managing the Fund: <N.N> Years
- Date format: "Month DD, YYYY" *with* the day (matching HDFC, unlike SBI/Nippon).
- "Mr./Ms." honorifics standard.
- **No lead/co labeling.** With multi-manager schemes the picker falls back
  to the locked "earliest start_date" rule (same as HDFC, SBI).

URL pattern (current, as of 2026-05-24):
  https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/factsheets/{YYYY}/absl-factsheet_{month-lower}-{YYYY}.pdf
  e.g. "absl-factsheet_may-2026.pdf" published in May 2026 reports April 2026
  data. Older months get removed from the listing once the next month
  publishes — i.e. **April 2026 was rotated off the server in late May 2026**.
  In 2025 the URL pattern was different ("abslmf_empower-<month>-<year>"
  with various separator inconsistencies).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Iterable

import pdfplumber

from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_MONTH_FULL = (
    "January|February|March|April|May|June|"
    "July|August|September|October|November|December"
)
_MONTH_INDEX = {
    name.lower(): i + 1 for i, name in enumerate(_MONTH_FULL.split("|"))
}

# Right-most pixel that's still "left column" on an ABSL scheme page.
# Verified gap: left column ends ~135, right column starts ~194.
_LEFT_COL_MAX_X = 175.0

# Per-block regex applied to a single line. Captures the name after
# "Fund Manager -Mr." / "Fund Manager - Mr." (literal space-dash plus optional
# space then honorific). Allows up to 4 capitalized tokens for the name.
_NAME_LINE_RE = re.compile(
    r"""
    ^\s*
    Fund\s+Manager\s*-\s*
    (?:Mr|Ms|Mrs|Dr)\.\s*
    (?P<name>[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,4})
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Matches the start-date line. Day is required (ABSL always prints it).
_DATE_LINE_RE = re.compile(
    rf"""
    ^\s*
    Managing\s+the\s+Fund\s+Since\s*:?\s*
    (?P<month>{_MONTH_FULL})\s+(?P<day>\d{{1,2}})\s*,?\s*(?P<year>\d{{4}})
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Scheme-name detection: page lines starting with "Aditya Birla Sun Life "
# ending in "Fund" or "Plan". The name may wrap across 2 lines (e.g. a long
# scheme name like "Aditya Birla Sun Life Balanced\nAdvantage Fund"), so the
# parser joins the first few non-empty page lines before matching.
_SCHEME_NAME_RE = re.compile(
    # Allow Unicode left/right quotes ' ' " " in names like "Equity Hybrid '95 Fund".
    r"^(Aditya\s+Birla\s+Sun\s+Life\s+[A-Za-z][A-Za-z0-9 &'‘’“”/().\-]*?\s+(?:Fund|Plan))\b",
    re.IGNORECASE,
)


def _publish_ym(data_ym: str) -> str:
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{m + 1:02d}"


def _parse_md_y(month_str: str, day_str: str, year_str: str) -> date | None:
    m = _MONTH_INDEX.get(month_str.lower())
    if not m:
        return None
    try:
        return date(int(year_str), m, int(day_str))
    except ValueError:
        return None


@register_adapter
class AbslAdapter(ManagerAdapter):
    """Aditya Birla Sun Life Mutual Fund factsheet adapter."""

    amc_slug = "absl"
    source_label = "Aditya Birla Sun Life Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Build the canonical PDF URL.

        ABSL names the file by the PUBLISH month, not the data month. So a
        factsheet covering April 2026 data is published in May 2026 as
        'absl-factsheet_may-2026.pdf'. We accept ym as the data month and
        compute the publish month for the URL.
        """
        publish_ym = _publish_ym(ym)
        pub_y, pub_m = map(int, publish_ym.split("-"))
        month_lower = _MONTH_FULL.split("|")[pub_m - 1].lower()
        return (
            "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/"
            f"resources/factsheets/{pub_y}/absl-factsheet_{month_lower}-{pub_y}.pdf"
        )

    # -------------------------------------------------------------------
    # PTR extraction (Phase 2.2.H)
    # ABSL: "Portfolio Turnover 0.49" — value already in fraction.
    # -------------------------------------------------------------------

    _ABSL_PTR_RE = re.compile(r"Portfolio\s+Turnover\s+(\d+\.\d+)", re.IGNORECASE)

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = self._scheme_name_from_page(text)
                if not scheme:
                    continue
                m = self._ABSL_PTR_RE.search(text)
                if not m:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=float(m.group(1)),
                    source_amc=self.amc_slug,
                )

    @staticmethod
    def _scheme_name_from_page(text: str) -> str | None:
        """Find the scheme name on the page.

        Scheme name lines start with "Aditya Birla Sun Life" and end with
        "Fund" or "Plan". The name occasionally wraps to 2 visual lines
        (e.g. "Aditya Birla Sun Life Balanced\nAdvantage Fund April 2026"),
        so we try the first 6 lines individually AND each 2-line and 3-line
        join. The first successful match wins.
        """
        if not text:
            return None
        lines = [l.strip() for l in text.splitlines() if l.strip()][:6]
        # Try each line, then each 2-line and 3-line concatenation.
        candidates: list[str] = []
        for i in range(len(lines)):
            candidates.append(lines[i])
            if i + 1 < len(lines):
                candidates.append(lines[i] + " " + lines[i + 1])
            if i + 2 < len(lines):
                candidates.append(lines[i] + " " + lines[i + 1] + " " + lines[i + 2])
        for cand in candidates:
            m = _SCHEME_NAME_RE.match(cand)
            if m:
                return m.group(1).strip()
        return None

    @staticmethod
    def _left_col_lines(words: list[dict]) -> list[str]:
        """Group left-column words into one string per visual line."""
        left = [w for w in words if w["x0"] < _LEFT_COL_MAX_X]
        if not left:
            return []
        left.sort(key=lambda w: (w["bottom"], w["x0"]))
        rows: list[list[dict]] = []
        for w in left:
            if rows and abs(w["bottom"] - rows[-1][-1]["bottom"]) < 3:
                rows[-1].append(w)
            else:
                rows.append([w])
        out: list[str] = []
        for row in rows:
            row.sort(key=lambda w: w["x0"])
            out.append(" ".join(w["text"] for w in row))
        return out
