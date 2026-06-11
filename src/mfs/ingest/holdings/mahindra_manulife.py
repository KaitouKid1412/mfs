"""Mahindra Manulife monthly portfolio holdings adapter (Phase 5).

Like Nippon (and unlike HDFC/SBI), Mahindra Manulife publishes ONE
consolidated workbook per month covering every MMF scheme. The downloads
hub is a server-rendered page:

    https://www.mahindramanulife.com/downloads

whose static HTML already carries every disclosure as an anchor:

    <a download='Monthly-Portfolio-Disclosure---April,-2026.xlsx'
       href='uploads/download/<uuid>.xlsx' class='btn download ...'>

The human-readable DATA month lives in the ``download`` attribute (the
``href`` is an opaque UUID path), so we key discovery off the download
filename. The month in that filename is the DATA month-end ("as on April
30, 2026"), with no publish-month offset. The ``href`` is root-relative
and absolutizes against the mahindramanulife.com origin.

Workbook structure (validated against the April-2026 file, ~1.9 MB,
27 sheets):
- No "Index" sheet. Sheets are named by an internal code (``MMF01`` ...
  ``MMF28``); the printed scheme name lives in **row 2 (0-indexed),
  column B** of each sheet.
- Per-scheme layout:
    row 0: col A = sheet code, col B = "MAHINDRA MANULIFE MUTUAL FUND"
    row 1: col B = "Monthly Portfolio Statement as on April 30, 2026"
    row 2: col B = the scheme name (e.g. "Mahindra Manulife Multi Cap Fund")
    row 3: col B = SEBI scheme-type description
    row 5: header — col B="Name of the Instrument", col C="ISIN",
           col D="Industry* / Rating", col E="Quantity",
           col F="Market/Fair Value (Rs. in Lakhs)", col G="% to Net Assets",
           col H="Yield to Maturity"
    row 6+: section banners ("Equity & Equity related" / "Debt Instruments"
           / "Money Market Instruments" / "TREPS" / "Net Receivables ...")
           interleaved with ISIN-bearing holding rows, then "Sub Total" /
           "Total" / "GRAND TOTAL" footers, then NAV table + notes +
           riskometer block.

This is the canonical SEBI layout, so the shared single-sheet parser in
``_generic`` (``_parse_one_sheet``) auto-detects the header row, the
ISIN/name/weight columns, and the weight unit (Mahindra prints "% to Net
Assets" in **percent**, e.g. 3.70). We only override ``parse_excel`` to
pick the correct sheet out of the consolidated workbook by matching the
scheme name in row 2 — exactly the Nippon pattern, minus the Index sheet.
"""

from __future__ import annotations

import html as _html
import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urljoin

import openpyxl

from mfs import paths
from mfs.ingest.holdings._generic import GenericHoldingsAdapter, _parse_one_sheet
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import download_to, fetch_bytes
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_ORIGIN = "https://www.mahindramanulife.com"
_DISCLOSURE_PAGE = f"{_ORIGIN}/downloads"

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# The download anchor carries the DATA-month filename in `download` and the
# opaque UUID path in `href`. We capture both. The filename is e.g.
# "Monthly-Portfolio-Disclosure---April,-2026.xlsx" (the comma is literal).
# We keep the href tight (no quote/space) and absolutize it downstream.
_ANCHOR_RE = re.compile(
    r"<a\s+download='Monthly-Portfolio-Disclosure---"
    r"(?P<month>[A-Za-z]+),-(?P<year>\d{4})\.xlsx?'\s+"
    r"href='(?P<href>[^']+?\.xlsx?)'",
    re.IGNORECASE,
)


def _data_month_parts(ym: str) -> tuple[str, str]:
    """data_ym='2026-04' -> ('April', '2026'). Mahindra keys the monthly
    portfolio by the DATA month-end, so there is no publish-month offset."""
    y, m = map(int, ym.split("-"))
    return _MONTHS[m - 1], f"{y:04d}"


def _master_excel_filename(ym: str) -> str:
    """Canonical local-cache filename for the consolidated monthly Excel."""
    return f"Mahindra-Monthly-Portfolio-{ym}.xlsx"


def _scheme_name_from_sheet(ws) -> str | None:
    """Pull the printed scheme name from row 2 (0-indexed), column B.

    Returns None if the sheet doesn't look like a per-scheme portfolio
    sheet (e.g. a banner / metadata sheet without the expected layout).
    """
    rows = ws.iter_rows(min_row=3, max_row=3, values_only=True)
    try:
        row = next(iter(rows))
    except StopIteration:
        return None
    name = row[1] if len(row) > 1 else None
    if not isinstance(name, str):
        return None
    name = re.sub(r"\s+", " ", name).strip()
    return name or None


@register_adapter
class MahindraManulifeHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "mahindra_manulife"
    source_label = "Mahindra Manulife Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Find the consolidated workbook for `ym`, download it once, and
        enumerate the per-scheme names from each sheet's row-2 banner.

        Mahindra ships a SINGLE workbook for all schemes, so every entry in
        the returned dict maps to the same URL. `fetch_excel` short-circuits
        to the already-cached master file.
        """
        html = fetch_bytes(_DISCLOSURE_PAGE).decode("utf-8", errors="replace")
        master_url = self._select_master_url(html, ym)
        if master_url is None:
            log.warning("holdings.mahindra_manulife.no_url_for_ym", ym=ym)
            return {}

        master_path = self._cached_master_path(ym)
        if not master_path.exists():
            download_to(master_url, master_path)

        try:
            wb = openpyxl.load_workbook(
                master_path, read_only=True, data_only=True
            )
        except Exception as e:  # noqa: BLE001 — surface any payload issue
            log.error(
                "holdings.mahindra_manulife.workbook_open_failed",
                ym=ym, path=str(master_path), err=str(e),
            )
            return {}

        out: dict[str, str] = {}
        try:
            for sname in wb.sheetnames:
                scheme = _scheme_name_from_sheet(wb[sname])
                if not scheme:
                    continue
                out.setdefault(scheme, master_url)
        finally:
            wb.close()

        log.info(
            "holdings.mahindra_manulife.discover",
            ym=ym, n_schemes=len(out), master_url=master_url,
        )
        return out

    def _select_master_url(self, html: str, ym: str) -> str | None:
        """Pick the monthly-portfolio anchor whose download-filename month
        matches data month `ym`; absolutize its (root-relative) href."""
        month_name, year = _data_month_parts(ym)
        for m in _ANCHOR_RE.finditer(html):
            if m.group("month").lower() != month_name.lower():
                continue
            if m.group("year") != year:
                continue
            href = _html.unescape(m.group("href")).strip()
            # The href is root-relative ("uploads/download/<uuid>.xlsx").
            return urljoin(_ORIGIN + "/", href.lstrip("/"))
        return None

    def _cached_master_path(self, ym: str) -> Path:
        return paths.holdings_excel_raw(
            self.amc_slug, ym, _master_excel_filename(ym)
        )

    # ------------------------------------------------------------------
    # Fetch — return the cached consolidated workbook (shared by all schemes)
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Return the cached master workbook path for this month.

        Mahindra's master Excel is shared across all schemes in this ym, so
        `scheme_filename` is intentionally ignored. We still download here
        (idempotent) in case the pipeline invoked fetch without going
        through discover first.
        """
        master_path = self._cached_master_path(ym)
        if master_path.exists():
            return master_path
        return download_to(url, master_path)

    # ------------------------------------------------------------------
    # Parse — pick the scheme's sheet, then delegate to the shared parser
    # ------------------------------------------------------------------

    def parse_excel(
        self,
        excel_path: Path,
        scheme_name_printed: str,
        ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        """Locate the scheme's sheet inside the consolidated workbook (by
        the row-2 scheme name) and yield ISIN-bearing holding rows via the
        shared single-sheet SEBI parser."""
        wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        try:
            target = re.sub(r"\s+", " ", scheme_name_printed).strip().lower()
            ws = None
            for sname in wb.sheetnames:
                name = _scheme_name_from_sheet(wb[sname])
                if name and name.lower() == target:
                    ws = wb[sname]
                    break
            if ws is None:
                log.warning(
                    "holdings.mahindra_manulife.scheme_not_found",
                    scheme=scheme_name_printed, ym=ym,
                )
                return
            recs = _parse_one_sheet(ws)
            if not recs:
                return
            for r in recs:
                yield ParsedHoldingRecord(
                    scheme_name_printed=scheme_name_printed,
                    security_name=r.security_name,
                    weight_pct=r.weight_pct,
                    isin=r.isin,
                    instrument_type=r.instrument_type,
                    source_amc=self.amc_slug,
                )
        finally:
            wb.close()
