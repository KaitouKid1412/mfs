"""Abakkus Mutual Fund monthly portfolio holdings adapter (Phase 5).

Abakkus (Abakkus Asset Manager; SEBI MF approval Aug-2025) is a small AMC
with three schemes — Flexi Cap, Liquid, Small Cap. Like Nippon / ABSL (and
unlike HDFC's one-Excel-per-scheme shape), Abakkus publishes ONE consolidated
workbook per month carrying every scheme on its own tab.

Discovery
---------
The statutory-disclosures page

    https://www.abakkusmf.com/statutory-disclosures.html

is a STATIC server-rendered HTML page (no SPA/JSON API for this section). Its
"Monthly Portfolio Disclosure" block lists one entry per month as

    <h4 ...>April 30, 2026</h4>
    <a href="/uploads/IN_MF_MONTHLY_PORTFOLIO_April_30_2026_<hash>.xls" ...>Download</a>

The ``/uploads/`` filenames are Strapi uploads with a content-hash suffix AND
an unstable stem month-to-month (``Abakkus_MF_MONTHLY_PORTFOLIO_31_12_2025``,
``ABK_MF_MONTHLY_PORTFOLIO_February_28_2026``, ``IN_MF_MONTHLY_PORTFOLIO_
April_30_2026`` …), so the filename can never be keyed on the data month. We
therefore key discovery off the ``<h4>`` LABEL date (``<Month> <DD>, <YYYY>``,
the SEBI "as on" month-end), scoped to the "Monthly Portfolio Disclosure"
section so the Fortnightly / Half-Yearly blocks (same ``<h4>`` + ``<a>`` shape,
overlapping dates) can never leak in.

Because Abakkus ships ONE workbook for all schemes, every discovered scheme
maps to the SAME URL (Nippon/ABSL-style); ``fetch_excel`` short-circuits to
the once-downloaded cached file and the scheme list is read from the
workbook's Index sheet.

Workbook layout (validated against the April 30, 2026 file)
-----------------------------------------------------------
The download is a true legacy OLE2/BIFF ``.xls`` (magic ``\\xd0\\xcf\\x11\\xe0``),
which ``openpyxl`` cannot read — so we parse with ``xlrd`` (xlrd >= 2.0 reads
``.xls`` only, exactly right here) rather than the openpyxl-based shared
``parse_sebi_excel``. We still reuse the shared SEBI header detection /
section classification / ISIN check from ``_generic`` so the row-walking logic
stays identical to every other adapter:

- Sheet 0 ``Index``: col A = Sr No., col B = Short Name (== per-scheme sheet
  name, e.g. 'ABAFC'/'ABALI'/'ABASC'), col C = Scheme Name ('Abakkus Flexi
  Cap Fund').
- Sheets 1..N: one per scheme, sheet name == the Index Short Name.
  - rows 0-2: '<CODE> | Abakkus Mutual Fund' banner, the printed scheme name,
    'Monthly Portfolio Statement as on April 30, 2026'.
  - row 4: header — col B='Name of the Instrument', col C='ISIN', col
    D='Industry* / Rating', col E='Quantity', col F='Market/Fair Value (Rs.
    in Lakhs)', col G='% to Net Assets', col H='YTM', col I='YTC^'.
  - row 5+: section banners ('Equity & Equity related', '(a) Listed /
    awaiting listing on Stock Exchanges', …) interleaved with ISIN rows.
  - '% to Net Assets' is stored as a PERCENT (ICICI Bank 4.37 == 4.37%);
    Flexi Cap April-2026 has 51 equity ISIN rows summing to 93.4%.

The header position is auto-detected (col offsets are not hard-coded) so a
cosmetic column shift in a future month is handled by the shared
``_detect_header``. We hard-stop at a 'Grand Total' row — everything past it
is a benchmark/riskometer note block whose cells carry free text the strict
ISIN regex would already reject (the hard stop is a second guardrail).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urljoin

import xlrd

from mfs import paths
from mfs.ingest.holdings._base import HoldingsAdapter
from mfs.ingest.holdings._generic import (
    _detect_header,
    classify_section,
    is_isin,
)
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import download_to, fetch_bytes
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = "https://www.abakkusmf.com/statutory-disclosures.html"
_BASE = "https://www.abakkusmf.com"

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_ALT = "|".join(_MONTH_NAMES)

# The "Monthly Portfolio Disclosure" content section's <h2> heading, used to
# scope discovery away from the Fortnightly / Half-Yearly blocks (which share
# the same <h4>date</h4> + <a href> markup and have overlapping month dates).
_SECTION_RE = re.compile(
    r"<h2[^>]*>\s*Monthly Portfolio Disclosure\s*</h2>", re.IGNORECASE,
)
# The next <h2> after the Monthly section ends the slice we scan.
_NEXT_H2_RE = re.compile(r"<h2[^>]*>", re.IGNORECASE)

# Within the Monthly section: a "<Month> <DD>, <YYYY>" label (in an <h4>)
# immediately followed by the portfolio's /uploads/ .xls[x] download anchor.
# The date carries the SEBI month-end ("as on") date — our data-month anchor.
_ENTRY_RE = re.compile(
    rf"<h4[^>]*>\s*(?P<month>{_MONTH_ALT})\s+(?P<day>\d{{1,2}}),\s*"
    r"(?P<year>\d{4})\s*</h4>"
    r"\s*<a[^>]+href=\"(?P<url>(?:https://www\.abakkusmf\.com)?"
    r"/uploads/[^\"]+?\.xlsx?)(?:\?[^\"]*)?\"",
    re.IGNORECASE,
)


def _label_data_ym(month_name: str, year: str) -> str | None:
    """('April', '2026') -> '2026-04', or None if the month isn't recognized."""
    month_l = month_name.strip().lower()
    for i, n in enumerate(_MONTH_NAMES, start=1):
        if n.lower() == month_l:
            return f"{int(year):04d}-{i:02d}"
    return None


def _clean_name(raw: object) -> str | None:
    """Normalize a whitespace-noisy cell to a clean printed name, or None."""
    if not isinstance(raw, str):
        return None
    name = re.sub(r"\s+", " ", raw).strip()
    return name or None


@register_adapter
class AbakkusHoldingsAdapter(HoldingsAdapter):
    amc_slug = "abakkus"
    source_label = "Abakkus Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {printed_scheme_name: absolute_excel_url} for data month
        ``ym``.

        Scrape the Monthly-Portfolio-Disclosure section for the entry whose
        ``<h4>`` label date falls in ``ym``, download+open that consolidated
        workbook once, then enumerate every scheme from its Index sheet (each
        mapped to the same URL, Nippon/ABSL-style).
        """
        url = self._resolve_master_url(ym)
        if url is None:
            log.warning("holdings.abakkus.no_url_for_ym", ym=ym)
            return {}

        master_path = self._ensure_master_cached(url, ym)
        try:
            wb = xlrd.open_workbook(master_path)
        except Exception as e:  # noqa: BLE001 — surface any payload issue
            log.error(
                "holdings.abakkus.workbook_open_failed",
                ym=ym, path=str(master_path), err=str(e),
            )
            return {}

        sheet_set = set(wb.sheet_names())
        if "Index" not in sheet_set:
            log.warning(
                "holdings.abakkus.no_index_sheet",
                ym=ym, sheetnames=wb.sheet_names()[:5],
            )
            return {}

        out: dict[str, str] = {}
        idx = wb.sheet_by_name("Index")
        for r in range(idx.nrows):
            code = idx.cell_value(r, 1) if idx.ncols > 1 else None
            name = idx.cell_value(r, 2) if idx.ncols > 2 else None
            code_s = str(code).strip() if code else ""
            # Skip the header row ('Short Name'/'Scheme Name') and any code
            # that doesn't correspond to an actual scheme sheet.
            if not code_s or code_s not in sheet_set:
                continue
            scheme = _clean_name(name)
            if not scheme:
                continue
            out.setdefault(scheme, url)

        log.info("holdings.abakkus.discover", ym=ym, n_schemes=len(out), url=url)
        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_master_url(self, ym: str) -> str | None:
        """Scrape the disclosures HTML; return the consolidated monthly
        workbook URL whose Monthly-section ``<h4>`` label date matches ``ym``,
        else None."""
        html = fetch_bytes(_DISCLOSURE_PAGE).decode("utf-8", errors="replace")
        sec = _SECTION_RE.search(html)
        if sec is None:
            log.warning("holdings.abakkus.no_monthly_section", ym=ym)
            return None
        # Scope to the Monthly section: from its <h2> to the next <h2>.
        nxt = _NEXT_H2_RE.search(html, sec.end())
        block = html[sec.end():(nxt.start() if nxt else len(html))]

        for m in _ENTRY_RE.finditer(block):
            if _label_data_ym(m.group("month"), m.group("year")) != ym:
                continue
            url = m.group("url")
            if url.startswith("/"):
                url = urljoin(_BASE + "/", url.lstrip("/"))
            return url
        return None

    def _cached_master_path(self, ym: str) -> Path:
        """Local cache path for the consolidated workbook (always a BIFF
        .xls; we keep the .xls suffix since xlrd reads it directly)."""
        return paths.holdings_excel_raw(
            self.amc_slug, ym, f"abakkus-monthly-portfolio-{ym}.xls"
        )

    def _ensure_master_cached(self, url: str, ym: str) -> Path:
        """Download the consolidated workbook and cache it locally. Idempotent."""
        out = self._cached_master_path(ym)
        if out.exists():
            return out
        return download_to(url, out)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Return the cached consolidated workbook for this month.

        The workbook is shared across all schemes in ``ym``, so
        ``scheme_filename`` is intentionally ignored. We still download here
        (idempotent) so a caller invoking fetch without discover first works.
        """
        out = self._cached_master_path(ym)
        if out.exists():
            return out
        return self._ensure_master_cached(url, ym)

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def parse_excel(
        self,
        excel_path: Path,
        scheme_name_printed: str,
        ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        """Walk the scheme's sheet inside the consolidated workbook and yield
        ISIN-bearing holding rows. '% to Net Assets' is already a percent, so
        weights pass through unscaled."""
        wb = xlrd.open_workbook(excel_path)
        sheet_code = self._find_sheet_for_scheme(wb, scheme_name_printed)
        if sheet_code is None:
            log.warning(
                "holdings.abakkus.scheme_not_in_index",
                scheme=scheme_name_printed, ym=ym,
            )
            return
        ws = wb.sheet_by_name(sheet_code)

        # Materialize the sheet as row tuples and reuse the shared SEBI header
        # detector so column offsets are not hard-coded.
        rows = [
            tuple(ws.cell_value(r, c) for c in range(ws.ncols))
            for r in range(ws.nrows)
        ]
        detected = _detect_header(rows)
        if detected is None:
            log.warning(
                "holdings.abakkus.no_header",
                scheme=scheme_name_printed, ym=ym, sheet=sheet_code,
            )
            return
        header_idx, cm = detected
        isin_c, name_c, weight_c = cm["isin"], cm["name"], cm["weight"]

        current_section = "Equity"  # safe default if a banner is missing
        seen: set[str] = set()
        for row in rows[header_idx + 1:]:
            name = row[name_c] if name_c < len(row) else None
            isin = row[isin_c] if isin_c < len(row) else None
            weight = row[weight_c] if weight_c < len(row) else None

            name_s = name.strip() if isinstance(name, str) else ""
            isin_s = str(isin).strip() if isin is not None else ""

            # Hard stop at GRAND TOTAL: everything after is benchmark /
            # riskometer notes whose cells carry free text.
            if name_s.lower() == "grand total":
                break

            # Section banner / footer: name present, ISIN col is not an ISIN.
            if name_s and not is_isin(isin_s):
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
                weight_pct=w,  # already a percent (4.37 == 4.37%)
                isin=isin_s,
                instrument_type=current_section,
                source_amc=self.amc_slug,
            )

    @staticmethod
    def _find_sheet_for_scheme(
        wb: xlrd.book.Book, scheme_name_printed: str
    ) -> str | None:
        """Reverse-lookup the per-scheme sheet code (== Index Short Name) from
        the Index sheet by case-insensitive scheme-name match."""
        if "Index" not in wb.sheet_names():
            return None
        target = re.sub(r"\s+", " ", scheme_name_printed.strip()).lower()
        idx = wb.sheet_by_name("Index")
        sheet_set = set(wb.sheet_names())
        for r in range(idx.nrows):
            code = idx.cell_value(r, 1) if idx.ncols > 1 else None
            name = idx.cell_value(r, 2) if idx.ncols > 2 else None
            code_s = str(code).strip() if code else ""
            if not code_s or code_s not in sheet_set:
                continue
            clean = _clean_name(name)
            if clean and clean.lower() == target:
                return code_s
        return None
