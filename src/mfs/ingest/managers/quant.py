"""quant Mutual Fund — PTR adapter (Phase 5, abridged annual report).

quant **deliberately omits the Portfolio Turnover Ratio from its monthly
factsheet** (page 11 of the April-2026 issue carries an essay arguing PTR is
"an irrelevant measure"; page 74 repeats the stance, printing Sharpe /
Sortino / Jensen's Alpha instead). SEBI does not mandate PTR in the factsheet
— only as a per-scheme footnote in the **abridged scheme-wise annual report**
(Circular MFD/CIR/14/18337/2002, tied to Reg. 59A of the MF Regulations). So
this adapter sources quant's PTR from that annual report rather than the
factsheet. The number is the same trailing-period SEBI metric every other AMC
prints monthly — quant just publishes it annually.

Source document
---------------
The abridged annual report lives at a slug-stable URL keyed by fiscal year::

    https://quantmutual.com/Admin/disclouser/
        Annual-Report__quant-Mutual-Fund_Financial-Year-2023-24.pdf

The site's "Annual Report of Schemes" page is an ASP.NET WebForms app that
renders the document list via client-side postbacks (no static link / JSON
API), so ``fetch`` probes the fiscal-year URLs newest→oldest and downloads
the latest one that resolves (HTTP 200, ``application/pdf``). It auto-upgrades
the moment quant posts a newer FY at the same URL pattern. As of mid-2026 the
latest pattern-discoverable report is **FY2023-24** (period ended 31 Mar 2024);
FY2024-25 was not reachable at any guessable URL (it sits behind the WebForms
postback) — see ``docs/phase5/coverage_to_95.md`` for the freshness caveat and
the half-yearly-portfolio upgrade path.

Layout findings driving the parser
-----------------------------------
The 74-page abridged report carries a "Perspective Historical Per Unit
Statistics" block — 6 schemes per page across ~4 pages — laid out as one
COLUMN PER SCHEME with two sub-columns each (current FY + previous FY)::

    row 0  : ['', 'quant ABSOLUTE FUND', None, 'quant ESG EQUITY FUND', None, ...]
    period : ['', 'Period ended 31 March 2024', 'Period ended 31 March 2023', ...]
    ...
    PTR row: ['6. Portfolio turnover ratio4', '1.44', '1 .45', '2.75', '2 .66', ...]

``page.extract_tables()`` recovers this grid cleanly. For each scheme the
**current-FY** value is the first (odd-indexed) sub-column and parses clean;
only the previous-FY sub-column carries pdfplumber digit-mangling ('1 .45'),
which we never read. The PTR is printed in "times" (e.g. 1.44 = 144 %), which
is already our storage convention (fraction), so no /100 normalization. Debt
schemes (Liquid / Gilt / Overnight) print '-' and are skipped. ``as_of_month``
is read from the table's "Period ended 31 March YYYY" header so the value is
stamped at its true FY-end regardless of which FY file was fetched — see the
``as_of_month`` override on ``ParsedPtrRecord``.

Holdings stay on the parallel ISIN-tagged Excel path (``ingest/holdings``);
``parse_holdings`` here yields nothing.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Iterable

import httpx
import pdfplumber

from mfs.errors import IngestError
from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)


_MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        1,
    )
}

_SCHEME_RE = re.compile(r"^\s*quant\b.*\b(?:fund|fof)\b", re.IGNORECASE)
_PERIOD_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")

_AR_URL = (
    "https://quantmutual.com/Admin/disclouser/"
    "Annual-Report__quant-Mutual-Fund_Financial-Year-{fy}.pdf"
)

# The FY2023-24 report prints some legacy / abbreviated scheme names. Map the
# genuine renames to their current scheme_master spelling so the fuzzy matcher
# resolves them at full score, and skip report-only schemes that no longer
# exist in scheme_master — they would otherwise fuzzy-poach a live sibling
# (e.g. the discontinued "quant Absolute Fund" scores 86 against "quant Value
# Fund" and would overwrite Value's true PTR). Keys are lowercased printed names.
_NAME_FIXUPS = {
    "quant flexicap fund": "quant Flexi Cap Fund",
    "quant esg equity fund": "quant ESG Integration Strategy Fund",
}
_SKIP_NAMES = {
    "quant absolute fund",
    "quant active fund",
}


def _collapse(s: object) -> str:
    """Collapse internal whitespace/newlines in a table cell to single spaces."""
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def _clean_scheme_name(cell: object) -> str:
    """Return a collapsed scheme name if the cell is a quant scheme header,
    else ''. 'quant BUSINESS\\nCYCLE FUND' -> 'quant BUSINESS CYCLE FUND'."""
    name = _collapse(cell)
    return name if _SCHEME_RE.match(name) else ""


def _clean_ptr(cell: object) -> float | None:
    """Parse a PTR 'times' value. '-' / blank / non-numeric -> None. The
    current-FY sub-column is clean; we never read the mangled previous-FY one."""
    s = _collapse(cell).replace(" ", "")
    if s in ("", "-", "–", "NA", "N.A.", "N.A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _scheme_cell_count(row: list) -> int:
    return sum(1 for c in row if _clean_scheme_name(c))


def _period_end(period_row: list | None, page_text: str) -> date | None:
    """Resolve the table's current-FY period-end (month-floored). Prefer the
    'Period ended 31 March YYYY' header cells; fall back to scanning page text."""
    candidates: list[str] = []
    if period_row:
        candidates.extend(_collapse(c) for c in period_row if c and "ended" in str(c).lower())
    candidates.append(page_text)
    best: date | None = None
    for text in candidates:
        for m in _PERIOD_RE.finditer(text):
            mon = _MONTHS.get(m.group(2).title())
            if not mon:
                continue
            d = date(int(m.group(3)), mon, 1)
            # The header lists current FY then previous FY; take the latest.
            if best is None or d > best:
                best = d
        if best is not None:
            return best
    return best


@register_adapter
class QuantAdapter(ManagerAdapter):
    """quant Mutual Fund PTR adapter, sourced from the abridged annual report.

    ``amc_slug = 'quant'`` matches ``scheme_master.amc_code`` exactly, so no
    alias entry in ``_scheme_match.py`` is required. The lowercase branding is
    the AMC's actual name, not a typo.
    """

    amc_slug = "quant"
    source_label = "quant Mutual Fund"

    # ------------------------------------------------------------------
    # Fiscal-year URL resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _candidate_fy_labels(ym: str) -> list[str]:
        """Fiscal-year labels (e.g. '2023-24') to probe, newest first.

        Indian FY runs Apr–Mar; FY 'YYYY-YY' ends 31 March of the later year.
        For a run month ``ym`` we start at the FY ending in the most recent
        March on/before that month and walk back six years.
        """
        y, m = map(int, ym.split("-"))
        end_year = y if m >= 4 else y - 1
        return [f"{ey - 1}-{ey % 100:02d}" for ey in range(end_year, end_year - 6, -1)]

    @classmethod
    def _ar_url(cls, fy_label: str) -> str:
        return _AR_URL.format(fy=fy_label)

    def build_url(self, ym: str) -> str:
        """Deterministic newest-candidate annual-report URL (fetch() probes
        older years as a fallback)."""
        return self._ar_url(self._candidate_fy_labels(ym)[0])

    def fetch(self, ym: str) -> Path:
        """Return the local path to quant's latest available abridged annual
        report, probing fiscal-year URLs newest→oldest.

        Reuses a cached PDF if one exists for any candidate FY. If no report is
        reachable, raises ``IngestError`` — we never fabricate a PTR (fail-fast,
        no manual entry).
        """
        from mfs import paths
        from mfs.config import get_settings
        from mfs.io.http import download_to

        labels = self._candidate_fy_labels(ym)
        for fy in labels:
            cached = paths.annual_report_raw(self.amc_slug, fy)
            if cached.exists():
                return cached

        s = get_settings()
        with httpx.Client(
            timeout=s.http_timeout,
            headers={"User-Agent": s.user_agent},
            follow_redirects=True,
        ) as c:
            for fy in labels:
                url = self._ar_url(fy)
                try:
                    r = c.head(url)
                except httpx.HTTPError:
                    continue
                if r.status_code == 200 and "pdf" in r.headers.get("content-type", "").lower():
                    out = paths.annual_report_raw(self.amc_slug, fy)
                    log.info("quant.annual_report.selected", fy=fy, url=url)
                    return download_to(url, out)

        raise IngestError(
            "quant: no abridged annual report reachable at the known URL "
            f"pattern for FYs {labels[0]}..{labels[-1]}. quant omits PTR from "
            "its factsheet, so PTR cannot be sourced without the annual report."
        )

    # ------------------------------------------------------------------
    # PTR — from the annual report's per-scheme key-statistics table
    # ------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        """Yield one ParsedPtrRecord per scheme that prints a current-FY PTR.

        Each record is stamped with the report's true FY-end (read from the
        table header), not the run month, via ``as_of_month``.
        """
        records: list[ParsedPtrRecord] = []
        seen: set[str] = set()
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                for tbl in page.extract_tables():
                    if not tbl or len(tbl) < 3:
                        continue
                    ptr_row = next(
                        (r for r in tbl if r and r[0] and "portfolio turnover" in str(r[0]).lower()),
                        None,
                    )
                    if ptr_row is None:
                        continue
                    header_row = max(tbl, key=_scheme_cell_count)
                    if _scheme_cell_count(header_row) == 0:
                        continue
                    period_row = next(
                        (r for r in tbl if r and any(c and "ended" in str(c).lower() for c in r)),
                        None,
                    )
                    period = _period_end(period_row, page_text)
                    if period is None:
                        continue
                    # Current-FY values sit in the odd (first-of-pair) columns.
                    for j in range(1, len(header_row), 2):
                        name = _clean_scheme_name(header_row[j])
                        if not name:
                            continue
                        key = name.lower()
                        if key in _SKIP_NAMES:
                            continue
                        name = _NAME_FIXUPS.get(key, name)
                        if name in seen:
                            continue
                        ptr = _clean_ptr(ptr_row[j]) if j < len(ptr_row) else None
                        if ptr is None or not (0.0 < ptr < 20.0):
                            continue
                        seen.add(name)
                        records.append(
                            ParsedPtrRecord(
                                scheme_name_printed=name,
                                ptr=ptr,
                                source_amc=self.amc_slug,
                                as_of_month=period,
                            )
                        )
        log.info("quant.ptr.parsed", n_records=len(records))
        return records

    # ------------------------------------------------------------------
    # Holdings — covered by the parallel ISIN-tagged Excel path.
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
