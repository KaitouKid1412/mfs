"""Nippon India monthly portfolio holdings adapter (Phase 3.C).

Unlike HDFC (one Excel per scheme), Nippon India publishes ONE consolidated
workbook per month covering every NIMF scheme:

    https://mf.nipponindiaim.com/InvestorServices/FactsheetsDocuments/
        NIMF-MONTHLY-PORTFOLIO-{DD}-{Month}-{YY}.xls

…where:
- DD  = month-end day (28 / 29 / 30 / 31 depending on month length)
- Month = mix of full names ("April", "June", "July") and abbreviations
  ("Jan", "Feb", "Mar", "May", "Aug", "Sep", "Oct", "Nov", "Dec"); the
  pattern is whatever Nippon's web team typed on upload day, so the regex
  must accept both.
- YY  = two-digit year (e.g., "26" for 2026).

Despite the `.xls` extension, the response body is a regular .xlsx zip
(Office Open XML); openpyxl rejects it under the `.xls` name, so we cache
locally with an `.xlsx` extension. We confirmed via `file(1)`: "Microsoft
Excel 2007+", and the ZIP central directory contains `[Content_Types].xml`
plus `xl/workbook.xml` exactly like an .xlsx.

Workbook structure (validated against April 2026 file, ~1.4 MB):
- Sheet 0 is named `Index`: column A = 2-char sheet code, column B =
  full scheme name with parenthesised description (and sometimes
  segregated-portfolio names on subsequent lines).
- Sheets 1..N: one sheet per scheme, sheet name == the 2-char index code.
- Per-scheme layout:
    row 0: scheme name banner (col A=internal code "RLMFnnn")
    row 1: "Monthly Portfolio Statement as on April 30,2026"
    row 2: blank
    row 3: header — col B="ISIN", col C="Name of the Instrument",
           col D="Industry / Rating", col E="Quantity",
           col F="Market/Fair Value ( Rs. in Lacs)",
           col G="% to NAV", col H="YIELD"
    row 4+: section banners + holding rows.
- Banner rows: ISIN col (B) is blank; name col (C) carries the banner
  text. Convention: "Equity & Equity related" → "(a) Listed / awaiting
  listing on Stock Exchanges" → equity rows → "Subtotal" → optional
  illiquid sub-block → "(b) UNLISTED" → "Total" → "Money Market
  Instruments" → "Triparty Repo/ Reverse Repo Instrument" → "OTHERS"
  → "Net Current Assets" → "GRAND TOTAL".
- Some hybrid schemes also have a "Debt Instruments" / "REIT" / "InvIT"
  section before "Money Market Instruments".
- A small number of legacy schemes append a SECOND Yes-Bank "Segregated
  Portfolio 2" mini-table after GRAND TOTAL — we MUST stop reading at the
  first GRAND TOTAL to avoid double-counting.

CRITICAL UNIT NOTE: Nippon stores "% to NAV" as a **fraction**, not a
percentage. HDFC Bank in Large Cap shows up as `0.0924` for a 9.24% weight.
We multiply by 100 to convert to the canonical percent representation that
`ParsedHoldingRecord.weight_pct` expects.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote

import openpyxl

from mfs import paths
from mfs.io.http import download_to, fetch_bytes
from mfs.ingest.holdings._base import HoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = (
    "https://mf.nipponindiaim.com/investor-service/downloads/"
    "factsheet-and-other-portfolio-disclosures"
)
_FILES_HOST_PATH = (
    "https://mf.nipponindiaim.com/InvestorServices/FactsheetsDocuments/"
)

# Month tokens that appear in URLs. Order matters: try the long form before
# the short form so e.g. "April" doesn't fall through to "Apr".
_MONTH_TOKENS: dict[int, list[str]] = {
    1: ["January", "Jan"],
    2: ["February", "Feb"],
    3: ["March", "Mar"],
    4: ["April", "Apr"],
    5: ["May"],
    6: ["June", "Jun"],
    7: ["July", "Jul"],
    8: ["August", "Aug"],
    9: ["September", "Sep", "Sept"],
    10: ["October", "Oct"],
    11: ["November", "Nov"],
    12: ["December", "Dec"],
}

# Matches the full NIMF monthly Excel URL. Day, month name, and 2-digit
# year are captured separately so we can verify alignment with the
# requested data month.
#
# Examples seen on the live disclosures page:
#   NIMF-MONTHLY-PORTFOLIO-30-April-26.xls
#   NIMF-MONTHLY-PORTFOLIO-28-Feb-26.xls
#   NIMF-MONTHLY-PORTFOLIO-31-Jan-26.xls
#   NIMF-MONTHLY-PORTFOLIO-30-Sep-25.xls
#
# We deliberately keep this anchored to "NIMF-MONTHLY-PORTFOLIO" and require
# `.xls` to avoid colliding with fortnightly / debt-only files on the same
# page (e.g. NIMF-FORTNIGHTLY-PORTFOLIO-*, Debt-Portfolio-*).
_URL_RE = re.compile(
    # The disclosures page sometimes serves absolute URLs and sometimes
    # relative paths starting at `/InvestorServices/…`. Both must match.
    r'((?:https://mf\.nipponindiaim\.com)?/InvestorServices/FactsheetsDocuments/'
    r'NIMF[_-]MONTHLY[_-]PORTFOLIO[_-]'
    r'(?P<day>\d{1,2})[_-]'
    r'(?P<month>[A-Za-z]+)[_-]'
    r'(?P<yy>\d{2})\.xls)',
    re.IGNORECASE,
)

_NIPPON_HOST = "https://mf.nipponindiaim.com"

# Some older filenames also use "NIMF-MONTHLY-PORTFOLIO-<Month>-<YYYY>.xls"
# (e.g. NIMF-MONTHLY-PORTFOLIO-APRIL-2024.xls). We don't try to match those —
# they predate the period we care about and the URL shape is different.


def _publish_ym(data_ym: str) -> str:
    """Data month → publish month.

    Nippon publishes the monthly portfolio inside the same data month
    (typically by the 10th of the *next* calendar month); but unlike HDFC
    the URL path itself doesn't encode a publish month — the date in the
    filename IS the data-month-end. We keep this helper for symmetry with
    the HDFC adapter and for the off-chance Nippon ever switches to an
    ABSL/Mirae-style "publish = data + 1" path.
    """
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{m + 1:02d}"


def _classify_section(label: str) -> str | None:
    """Map a section banner string to an instrument_type.

    Returns None if the label is empty or not a recognized banner — caller
    should keep the previous section in that case (or, more conservatively,
    skip the row).
    """
    if not label:
        return None
    s = label.strip().lower()
    # Skip pure subtotal / total / grand total rows — those are footers,
    # not section starts. The caller will handle them separately.
    if s in ("subtotal", "total", "grand total"):
        return None
    if "equity" in s and "derivative" not in s:
        return "Equity"
    if any(k in s for k in (
        "debt", "money market", "triparty repo", "tri-party repo",
        "treasury", "government securities", "bonds", "debentures",
        "certificate of deposit", "commercial paper",
        "zero coupon", "preference shares", "non convertible",
        "securitised debt",
    )):
        return "Debt"
    if "reit" in s or "invit" in s:
        return "REIT/InvIT"
    if any(k in s for k in (
        "net current asset", "cash", "tbill", "t-bill",
        "others", "cash margin",
    )):
        return "Cash"
    return None


_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def _is_isin(s: str) -> bool:
    """Strict ISIN check: 2 letters + 9 alphanumeric + 1 check digit."""
    return bool(_ISIN_RE.match(s))


def _parse_filename_ym(filename: str) -> str | None:
    """Reverse-derive data_ym (YYYY-MM) from a NIMF monthly filename.

    Returns None if the filename doesn't parse. Year is normalized: a
    2-digit year < 70 → 20YY, else 19YY (we'll never realistically need
    the 19xx branch).
    """
    m = _URL_RE.search(filename) or _URL_RE.search(
        _FILES_HOST_PATH + filename
    )
    if not m:
        return None
    month_name = m.group("month")
    month = _month_name_to_int(month_name)
    if month is None:
        return None
    yy = int(m.group("yy"))
    year = 2000 + yy if yy < 70 else 1900 + yy
    return f"{year:04d}-{month:02d}"


def _month_name_to_int(name: str) -> int | None:
    """Map an English month name (full or abbreviated) to 1..12.

    Case-insensitive. Accepts both 'April' and 'Apr'; also tolerates
    'Sept' which Nippon occasionally uses.
    """
    n = name.strip().lower()
    for month_int, tokens in _MONTH_TOKENS.items():
        for t in tokens:
            if n == t.lower():
                return month_int
    return None


def _master_excel_filename(ym: str) -> str:
    """Canonical local-cache filename for the consolidated monthly Excel.

    Nippon ships ONE workbook for all schemes per month. We rename .xls →
    .xlsx because the file is an OOXML zip despite the upstream extension
    and openpyxl refuses the .xls suffix.
    """
    return f"NIMF-MONTHLY-PORTFOLIO-{ym}.xlsx"


def _extract_scheme_name_from_index_row(raw: object) -> str | None:
    """Pull the bare scheme name out of an Index-sheet column-B cell.

    The cell contains the official scheme name followed by a
    parenthesised SEBI-style description, and occasionally a second
    newline-separated segregated-portfolio name. We want only the first
    name, stripped of the description.

    Returns None for empty / non-string input.
    """
    if not isinstance(raw, str):
        return None
    first_line = raw.split("\n")[0].strip()
    if not first_line:
        return None
    m = re.match(r"^(.+?)\s*\(", first_line)
    name = m.group(1).strip() if m else first_line
    # Collapse any internal whitespace runs.
    return re.sub(r"\s+", " ", name)


@register_adapter
class NipponHoldingsAdapter(HoldingsAdapter):
    amc_slug = "nippon"
    source_label = "Nippon India Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Find the master Excel for `ym`, download it once, and enumerate
        the per-scheme entries from its Index sheet.

        Because Nippon publishes a SINGLE workbook for all schemes, every
        entry in the returned dict maps to the same URL. That URL is fine
        for `_run.py`'s `fetch_excel` step (which we override to short-
        circuit to the already-cached master file).
        """
        html = fetch_bytes(_DISCLOSURE_PAGE).decode("utf-8", errors="replace")
        master_url = self._select_master_url(html, ym)
        if master_url is None:
            log.warning(
                "holdings.nippon.no_url_for_ym",
                ym=ym,
            )
            return {}

        # Pre-cache the workbook so subsequent fetch_excel + parse_excel
        # calls are local-only. This is also the natural point to bail if
        # the upstream payload isn't a recognizable xlsx.
        master_path = self._cached_master_path(ym)
        if not master_path.exists():
            download_to(master_url, master_path)
        out: dict[str, str] = {}
        try:
            wb = openpyxl.load_workbook(master_path, read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001 — surface any payload issue
            log.error(
                "holdings.nippon.workbook_open_failed",
                ym=ym, path=str(master_path), err=str(e),
            )
            return {}

        try:
            if "Index" not in wb.sheetnames:
                log.warning(
                    "holdings.nippon.no_index_sheet",
                    ym=ym, sheetnames=wb.sheetnames[:5],
                )
                return {}
            idx_ws = wb["Index"]
            sheet_set = set(wb.sheetnames)
            for i, row in enumerate(idx_ws.iter_rows(values_only=True)):
                if i == 0:
                    continue  # the "INDEX" header
                code = row[0] if len(row) > 0 else None
                raw_name = row[1] if len(row) > 1 else None
                if not code:
                    continue
                code_s = str(code).strip()
                if code_s not in sheet_set:
                    # Index row points to a sheet that doesn't exist —
                    # silently skip; that's a Nippon-side data hiccup,
                    # not ours.
                    continue
                scheme_name = _extract_scheme_name_from_index_row(raw_name)
                if not scheme_name:
                    continue
                # First write wins for the rare case of duplicate names.
                out.setdefault(scheme_name, master_url)
        finally:
            wb.close()

        log.info(
            "holdings.nippon.discover",
            ym=ym, n_schemes=len(out), master_url=master_url,
        )
        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _select_master_url(self, html: str, ym: str) -> str | None:
        """Pick the monthly URL whose filename date falls in data month `ym`.

        Relative paths from the disclosures HTML are absolutized against
        the canonical Nippon host before returning.
        """
        for m in _URL_RE.finditer(html):
            url = m.group(1)
            if url.startswith("/"):
                url = _NIPPON_HOST + url
            filename = unquote(url.rsplit("/", 1)[-1])
            file_ym = _parse_filename_ym(filename)
            if file_ym == ym:
                return url
        return None

    def _cached_master_path(self, ym: str) -> Path:
        """Path under data/raw/holdings/nippon/<ym>/ for the master workbook."""
        return paths.holdings_excel_raw(
            self.amc_slug, ym, _master_excel_filename(ym)
        )

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Return the cached master workbook path for this month.

        Note: Nippon's master Excel is shared across all schemes in this
        ym, so `scheme_filename` is intentionally ignored. We still
        download here (idempotent) in case _run.py invoked us without
        going through discover first, e.g. in a test.
        """
        master_path = self._cached_master_path(ym)
        if master_path.exists():
            return master_path
        return download_to(url, master_path)

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def parse_excel(
        self,
        excel_path: Path,
        scheme_name_printed: str,
        ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        """Walk the per-scheme sheet inside the consolidated workbook and
        yield ISIN-bearing holding rows.

        Strategy:
        - Open the workbook, look up the scheme's sheet code from the
          Index sheet, and process only that one sheet.
        - Skip header / section-banner / subtotal / total / grand-total
          rows. Track the most-recent section banner so each holding row
          carries an instrument_type.
        - Multiply col-G `% to NAV` by 100 because Nippon stores it as a
          fraction (0.0189 = 1.89%).
        - Stop processing immediately on the first GRAND TOTAL row to
          avoid bleeding into Yes-Bank segregated-portfolio mini-tables
          on the few legacy schemes that have them.
        """
        wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        try:
            sheet_code = self._find_sheet_for_scheme(wb, scheme_name_printed)
            if sheet_code is None:
                log.warning(
                    "holdings.nippon.scheme_not_in_index",
                    scheme=scheme_name_printed, ym=ym,
                )
                return
            ws = wb[sheet_code]
            current_section: str = "Equity"  # safe default if banner missing
            seen_isins: set[str] = set()
            for row in ws.iter_rows(values_only=True):
                cells = list(row) + [None] * max(0, 8 - len(row))
                isin = cells[1]
                name = cells[2]
                weight = cells[6]

                isin_s = str(isin).strip() if isin is not None else ""
                name_s = name.strip() if isinstance(name, str) else ""

                # Hard stop at GRAND TOTAL — anything after is footer or
                # segregated-portfolio table.
                if name_s.lower() == "grand total":
                    break

                # Banner row: name present, ISIN column empty / non-ISIN.
                if name_s and not _is_isin(isin_s):
                    section = _classify_section(name_s)
                    if section:
                        current_section = section
                    continue

                if not _is_isin(isin_s):
                    continue
                if not name_s:
                    continue
                try:
                    w = float(weight)
                except (TypeError, ValueError):
                    continue
                weight_pct = w * 100.0

                # Defensive dedupe: the same ISIN should never appear
                # twice in a single Nippon sheet, but if Nippon ever
                # publishes a Subtotal-with-ISIN row we don't want a
                # duplicate row sneaking through.
                if isin_s in seen_isins:
                    continue
                seen_isins.add(isin_s)

                # Strip the '**' suffix Nippon uses to mark non-traded /
                # illiquid securities (e.g. "Vedanta Aluminium Metal
                # Limited**"). We keep the suffix off the canonical name
                # but it's not load-bearing — it's just a presentation
                # marker.
                clean_name = name_s.rstrip(" *")

                yield ParsedHoldingRecord(
                    scheme_name_printed=scheme_name_printed,
                    security_name=clean_name,
                    weight_pct=weight_pct,
                    isin=isin_s,
                    instrument_type=current_section,
                    source_amc=self.amc_slug,
                )
        finally:
            wb.close()

    @staticmethod
    def _find_sheet_for_scheme(
        wb: openpyxl.Workbook, scheme_name_printed: str
    ) -> str | None:
        """Reverse-lookup the 2-char sheet code from the Index sheet.

        Match is case-insensitive on the bare scheme name (description
        and segregated-portfolio extras stripped). Returns the sheet
        code string, or None if not found.
        """
        if "Index" not in wb.sheetnames:
            return None
        target = scheme_name_printed.strip().lower()
        target = re.sub(r"\s+", " ", target)
        idx_ws = wb["Index"]
        sheet_set = set(wb.sheetnames)
        for i, row in enumerate(idx_ws.iter_rows(values_only=True)):
            if i == 0:
                continue
            code = row[0] if len(row) > 0 else None
            raw_name = row[1] if len(row) > 1 else None
            if not code:
                continue
            code_s = str(code).strip()
            if code_s not in sheet_set:
                continue
            name = _extract_scheme_name_from_index_row(raw_name)
            if name and name.lower() == target:
                return code_s
        return None
