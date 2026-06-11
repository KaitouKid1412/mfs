"""HSBC Mutual Fund monthly portfolio holdings adapter (Phase 5).

HSBC publishes ONE SEBI-format Excel per scheme per month. The disclosures
live in the "Fund Portfolios" accordion of the Sitecore "Information Library"
page, whose server-rendered HTML carries every monthly-portfolio link as a
plain root-relative URL — so a single page scrape gives us the whole month's
catalogue (HDFC-style static-HTML discovery), no backing JSON API needed:

    https://www.assetmanagement.hsbc.co.in/en/mutual-funds/investor-resources/
        information-library

Each link is a Sitecore media path:

    /-/media/files/attachments/india/mutual-funds/portfolios/
        document-{DDMMYYYY}/hsbc-{scheme-slug}-{DD-Mon-YYYY}.xlsx

CRITICAL: the ``document-{DDMMYYYY}`` folder is the UPLOAD/publish date (which
drifts month to month — e.g. the 31-May-2024 data sat in document-06062024),
while the human date in the FILENAME ("30-apr-2026") is the DATA month-end.
We therefore key discovery off the date PARSED FROM THE FILENAME, not the
folder, so the April-2026 DATA snapshot is selected for ym='2026-04'.

The filename date format drifts over HSBC's history — separators are usually
hyphens but occasionally absent ("31dec2021"), month tokens are full or
abbreviated and mixed-case ("30-April-2024", "30-November-2023"), and the
scheme slug sometimes runs straight into the date ("...div-yieldfund-30-apr-
2026"). We extract day/month/year with a tolerant trailing-date regex.

Links are root-relative (start at ``-/media/...`` with no leading slash); we
absolutize them against the canonical host.

Per-scheme Excel layout (validated against HSBC Flexi Cap / Large Cap /
Small Cap, April 2026):
- Sheet 0 holds the portfolio (sheet name = scheme code, e.g. 'HEIOPF');
  'Notes' + 'Disclaimer' sheets follow and are ignored.
- Row 0-3: AMC / scheme banner + "Portfolio Statement as of April 30, 2026".
- Row 5: header — col A (0)="Name of the Instrument", col B (1)="ISIN",
  col C (2)="Rating/Industries", col D (3)="Quantity",
  col E (4)="Market Value (Rs in Lacs)", col F (5)="Percentage to Net
  Assets", col G (6)="Yield...", col H (7)="YTC".
- Row 6+: section banners ("Equity & Equity Related Instruments", "Equity
  Shares", "Listed / Awaiting listing on Stock Exchanges", "Treps", "Net
  Current Assets...") carry text in col A with an empty ISIN col; holding
  rows carry a real ISIN in col B. "Total" / "Total Net Assets" footers and
  trailing risk-o-meter notes carry no ISIN, so they are never yielded.

We CANNOT reuse the shared ``parse_sebi_excel``: HSBC spells its weight
column "Percentage to Net Assets" (the word, no ``%`` glyph), which the
generic header detector — which requires a literal ``%`` in the weight
header — does not recognize, so it finds no header and yields nothing. We
therefore use a bespoke, column-aware parser (same pattern as the
kotak / icici_pru adapters), anchoring on the "ISIN" header row and reading
fixed offsets name=0, ISIN=1, weight=5.

CRITICAL UNIT NOTE: HSBC stores "Percentage to Net Assets" as a FRACTION
(ICICI Bank in Large Cap shows 0.0885 for 8.85%); we multiply by 100 to the
canonical percent representation ``ParsedHoldingRecord.weight_pct`` expects.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote

import openpyxl

from mfs import paths
from mfs.ingest.holdings._generic import (
    GenericHoldingsAdapter,
    classify_section,
    is_isin,
)
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import download_to, fetch_bytes
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_CANONICAL_HOST = "https://www.assetmanagement.hsbc.co.in"
_DISCLOSURE_PAGE = (
    f"{_CANONICAL_HOST}/en/mutual-funds/investor-resources/"
    "information-library"
)

# Any monthly-portfolio Excel under the Sitecore .../mutual-funds/portfolios/
# media path. Root-relative ("-/media/...") and absolute forms both match. We
# don't anchor the filename date here — it drifts too much — and parse the
# data month from the filename downstream. Anchoring to "/portfolios/" keeps us
# off the fortnightly-debt / weekly / half-yearly / scheme-summary folders.
_URL_RE = re.compile(
    r'((?:https?://[^"\'\s<>]+)?-?/media/[Ff]iles/attachments/india/'
    r'mutual-funds/portfolios/[^"\'\s<>]+?\.xlsx)',
    re.IGNORECASE,
)

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Trailing date in a portfolio filename. Day + month-name + 4-digit year, with
# optional hyphen separators (so "30-apr-2026", "30-April-2026",
# "30-November-2023", and "31dec2021" all parse). Anchored to the .xlsx tail so
# we read the DATA-month date, not a stray year inside a scheme name
# (e.g. "...gilt-june-2027-index-fund-30-apr-2026.xlsx" -> April 2026).
_DATE_TAIL_RE = re.compile(
    r"(?P<day>\d{1,2})-?"
    r"(?P<month>january|february|march|april|may|june|july|august|"
    r"september|sept|sep|jan|feb|mar|apr|jun|jul|aug|oct|nov|dec|"
    r"october|november|december)-?"
    r"(?P<year>\d{4})\.xlsx$",
    re.IGNORECASE,
)

# Fixed column offsets in HSBC's per-scheme sheet (0-indexed).
_COL_NAME = 0
_COL_ISIN = 1
_COL_WEIGHT = 5


def _filename_data_ym(filename: str) -> str | None:
    """Reverse-derive the DATA month (YYYY-MM) from a portfolio filename, or
    None if it has no parseable trailing date. The date in the name is the
    data month-end (April data -> '2026-04'), independent of the publish-date
    ``document-*`` folder it lives in.
    """
    m = _DATE_TAIL_RE.search(filename)
    if not m:
        return None
    month = _MONTHS.get(m.group("month").lower())
    if month is None:
        return None
    return f"{int(m.group('year')):04d}-{month:02d}"


def _scheme_from_filename(filename: str) -> str | None:
    """Derive a printed scheme name from the filename stem.

    Strips the trailing ``-{DD-Mon-YYYY}`` date and the leading ``hsbc-``,
    title-cases the slug, and prefixes "HSBC " for a stable, human-readable
    scheme name. The slug->amc_code resolution is handled by _scheme_match.py;
    this name is the discovery key + the printed label only.
    """
    stem = filename[:-5] if filename.lower().endswith(".xlsx") else filename
    # Drop the trailing date token (with or without separators).
    stem = _DATE_TAIL_RE.sub("", filename)
    stem = stem.rstrip("-")
    # Also strip a hanging date that survived without the .xlsx anchor.
    stem = re.sub(
        r"-?\d{1,2}-?(?:january|february|march|april|may|june|july|august|"
        r"september|sept|sep|jan|feb|mar|apr|jun|jul|aug|oct|nov|dec|"
        r"october|november|december)-?\d{4}$",
        "",
        stem,
        flags=re.IGNORECASE,
    ).rstrip("-")
    if not stem:
        return None
    if stem.lower().startswith("hsbc-"):
        stem = stem[len("hsbc-"):]
    elif stem.lower() == "hsbc":
        return None
    words = [w for w in stem.split("-") if w]
    if not words:
        return None
    name = "HSBC " + " ".join(w.capitalize() for w in words)
    return re.sub(r"\s+", " ", name).strip()


@register_adapter
class HsbcHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "hsbc"
    source_label = "HSBC Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Scrape the Information Library HTML and return {printed_scheme_name:
        absolute_excel_url} for every scheme whose filename-encoded DATA month
        equals ``ym``."""
        html = fetch_bytes(_DISCLOSURE_PAGE).decode("utf-8", errors="replace")
        out: dict[str, str] = {}
        seen_urls: set[str] = set()
        for mt in _URL_RE.finditer(html):
            url = mt.group(1)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            filename = unquote(url.rsplit("/", 1)[-1])
            if _filename_data_ym(filename) != ym:
                continue
            scheme = _scheme_from_filename(filename)
            if not scheme:
                continue
            out.setdefault(scheme, self._absolutize(url))
        log.info("holdings.hsbc.discover", ym=ym, n_schemes=len(out))
        return out

    @staticmethod
    def _absolutize(url: str) -> str:
        """Resolve a root-relative Sitecore media path to an absolute URL."""
        if url.startswith("http"):
            return url
        return _CANONICAL_HOST + "/" + url.lstrip("/")

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        out = paths.holdings_excel_raw(self.amc_slug, ym, scheme_filename)
        if out.exists():
            return out
        return download_to(url, out)

    # ------------------------------------------------------------------
    # Parse — bespoke because the weight header "Percentage to Net Assets"
    # has no '%' glyph, which defeats the shared generic header detector.
    # ------------------------------------------------------------------

    def parse_excel(
        self,
        excel_path: Path,
        scheme_name_printed: str,
        ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        try:
            ws = wb[wb.sheetnames[0]]  # portfolio is always sheet 0
            rows = list(ws.iter_rows(values_only=True))
            header_idx = self._find_header_row(rows)
            if header_idx is None:
                log.warning(
                    "holdings.hsbc.no_header",
                    scheme=scheme_name_printed, ym=ym,
                )
                return
            current_section = "Equity"
            seen: set[str] = set()
            ncols = _COL_WEIGHT + 1
            for row in rows[header_idx + 1:]:
                cells = list(row) + [None] * max(0, ncols - len(row))
                name = cells[_COL_NAME]
                isin = cells[_COL_ISIN]
                weight = cells[_COL_WEIGHT]

                name_s = name.strip() if isinstance(name, str) else ""
                isin_s = str(isin).strip() if isin is not None else ""

                # Section banner / footer: text in col A, col B not an ISIN.
                if name_s and not is_isin(isin_s):
                    low = name_s.lower()
                    if low in ("grand total", "total net assets") or \
                            low.startswith("total net assets"):
                        break
                    section = classify_section(name_s)
                    if section:
                        current_section = section
                    continue

                if not is_isin(isin_s) or not name_s:
                    continue
                try:
                    w = float(weight)
                except (TypeError, ValueError):
                    continue
                if isin_s in seen:
                    continue
                seen.add(isin_s)
                yield ParsedHoldingRecord(
                    scheme_name_printed=scheme_name_printed,
                    security_name=name_s.rstrip(" *"),
                    weight_pct=w * 100.0,  # HSBC stores weight as a fraction
                    isin=isin_s,
                    instrument_type=current_section,
                    source_amc=self.amc_slug,
                )
        finally:
            wb.close()

    @staticmethod
    def _find_header_row(rows: list[tuple]) -> int | None:
        """Locate the portfolio header row (the row carrying a bare 'ISIN'
        cell in col B). Returns its index, or None if not found in the first
        20 rows."""
        for i, row in enumerate(rows[:20]):
            cells = list(row)
            for c in cells[: _COL_ISIN + 2]:
                if isinstance(c, str) and c.strip().lower() == "isin":
                    return i
        return None
