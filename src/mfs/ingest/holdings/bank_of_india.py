"""Bank of India Mutual Fund monthly portfolio holdings adapter.

Like Nippon (and unlike HDFC's one-Excel-per-scheme), Bank of India
publishes ONE consolidated workbook per month covering every BOI MF scheme.

Discovery
---------
www.boimf.in is a Sitefinity SPA: the "Monthly Portfolio" tab on
https://www.boimf.in/investor-corner is populated client-side by an AJAX
POST (see the bundled AjaxCall.js → ``NoCategoryCall``):

    POST https://www.boimf.in/AjaxService.asmx/GetDocuments
    body: {"pagno":0,"category":null,"fromDate":null,"toDate":null,
           "LibraryName":"InvestorCorner","folderName":"MONTHLY PORTFOLIO",
           "CategoryValue":"no"}

The response is ``{"d": "<json-string>"}`` where the inner JSON has a
``Documents`` array of ``{DocName, FolderUrl, CustomFolderDate, ...}``.
One entry per month, e.g.:

    DocName   = "MONTHLY-PORTFOLIO - 30-APRIL-2026"
    FolderUrl = "https://www.boimf.in/docs/default-source/investorcorner/
                 monthly-portfolio/monthly-portfolio---30-april-2026.xlsx
                 ?sfvrsn=9c76e768_6"

We pick the entry whose filename date falls in the requested data month.
The ``?sfvrsn=`` cache-buster is dropped (the bare URL serves the file).

Workbook structure (validated against April-2026 file, ~580 KB, 26 sheets)
------------------------------------------------------------------------
- Sheet 0 ``Index``: col A = 2-char-ish scheme code ("YB01".."YB44"),
  col B = full scheme name + parenthesised SEBI description.
- Sheets 1..N: one per scheme, sheet name == the index code.
- A trailing ``F & O Disclosure`` sheet (no per-scheme portfolio table).
- Per-scheme layout (row indices 0-based):
    row 5: "Monthly Portfolio Statement as on April 30,2026" banner.
    row 6: header — col B="Name of the Instrument", col C="ISIN",
           col D="Industry / Rating", col E="Quantity",
           col F="Market/Fair Value (Rs. in Lacs)",
           col G="% to Net Assets", col H="YTM".
    row 7+: section banners ("Equity & Equity related", "(a) Listed /
            awaiting listing on Stock Exchanges", "DEBT INSTRUMENTS",
            "TREPS", "Net Receivable/Payable", ...) interleaved with
            ISIN-bearing holding rows. Trailing footnote rows ("4. Exposure
            to derivative ...", NAV legends) carry no ISIN and are ignored.

CRITICAL UNIT NOTE: BOI stores "% to Net Assets" as a **fraction**
(0.0444 = 4.44%), exactly like Nippon. We do NOT special-case this here —
the shared ``_parse_one_sheet`` detects fraction-vs-percent by total
magnitude and scales fractions up by 100, so it handles BOI's layout
out of the box (header auto-detected at row 6: name col 1, ISIN col 2,
weight col 6).

Because one workbook serves all schemes, every entry in
``discover_scheme_urls`` maps to the same consolidated URL, and
``parse_excel`` looks up the scheme's sheet via the Index sheet before
delegating to the generic single-sheet parser.
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
    _parse_one_sheet,
)
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import download_to
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_LISTING_API = "https://www.boimf.in/AjaxService.asmx/GetDocuments"
_INVESTOR_CORNER = "https://www.boimf.in/investor-corner"
_BOI_HOST = "https://www.boimf.in"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

# Matches the consolidated monthly-portfolio Excel filename, e.g.
# "monthly-portfolio---30-april-2026.xlsx". Day / month-name / year are
# captured so we can align the file with the requested data month-end.
_FILENAME_RE = re.compile(
    r"^monthly-portfolio---"
    r"(?P<day>\d{1,2})-"
    r"(?P<month>[a-z]+)-"
    r"(?P<year>\d{4})\.xlsx$",
    re.IGNORECASE,
)


def _strip_query(url: str) -> str:
    """Drop the Sitefinity ?sfvrsn=... cache-buster; the bare URL works."""
    return url.split("?", 1)[0]


def _file_ym(filename: str) -> str | None:
    """Reverse-derive data_ym (YYYY-MM) from a BOI monthly filename, or None."""
    m = _FILENAME_RE.match(filename)
    if not m:
        return None
    month_name = m.group("month").lower()
    if month_name not in _MONTH_NAMES:
        return None
    month = _MONTH_NAMES.index(month_name) + 1
    return f"{int(m.group('year')):04d}-{month:02d}"


def _extract_scheme_name(raw: object) -> str | None:
    """Pull the bare scheme name out of an Index-sheet column-B cell.

    The cell carries the official scheme name followed by a parenthesised
    SEBI description (sometimes on a new line, sometimes after extra
    spaces). We keep only the leading name, with internal whitespace runs
    collapsed.
    """
    if not isinstance(raw, str):
        return None
    first_line = raw.split("\n")[0].strip()
    if not first_line:
        return None
    m = re.match(r"^(.+?)\s*\(", first_line)
    name = m.group(1).strip() if m else first_line
    return re.sub(r"\s+", " ", name)


# scheme_master spells the two close-ended ELSS series inconsistently:
#   Series 1 -> "... Mid Cap Tax Fund Series 1" (two words, "Mid Cap")
#   Series 2 -> "... Midcap Tax Fund Series 2"  (one word, "Midcap")
# The monthly-portfolio Index sheet (and AMFI) print BOTH with the two-word
# "Mid Cap" spelling. For Series 2 that two-word form makes the shared
# token_set_ratio matcher tie at 100 against the open-ended "Bank of India
# Mid Cap Fund" (whose "MID CAP FUND" tokens are a subset), so the real
# Series 2 (canonical key "MIDCAP TAX FUND SERIES 2", token_set_ratio 90.9)
# is shut out of the 2-point tie-break window and Series 2 holdings get
# misattributed to the open-ended Mid Cap Fund. Collapsing ONLY Series 2's
# "Mid Cap" -> "Midcap" makes its canonical key exactly match scheme_master,
# scoring 100. Series 1 already matches at 100 with the two-word spelling, so
# it is deliberately left untouched (normalizing it would instead make it tie
# into the "MIDCAP TAX FUND SERIES" subset and mis-map to Series 2).
_SERIES2_SPELLING_RE = re.compile(
    r"\bMid\s+Cap(\s+Tax\s+Fund\s+Series\s+2)\b", re.IGNORECASE
)


def _normalize_for_match(name: str) -> str:
    """Align a printed BOI scheme name with scheme_master's spelling.

    Currently only fixes the Series-2 "Mid Cap" -> "Midcap" discrepancy; a
    no-op for every other scheme.
    """
    return _SERIES2_SPELLING_RE.sub(r"Midcap\1", name)


def _master_filename(ym: str) -> str:
    """Canonical local-cache name for the consolidated monthly workbook."""
    return f"monthly-portfolio-{ym}.xlsx"


@register_adapter
class BankOfIndiaHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "bank_of_india"
    source_label = "Bank of India Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Find the consolidated Excel for `ym`, cache it once, and enumerate
        per-scheme entries from its Index sheet.

        Every returned entry maps to the SAME consolidated URL (one workbook
        covers all schemes), mirroring the Nippon adapter.
        """
        master_url = self._select_master_url(ym)
        if master_url is None:
            log.warning("holdings.bank_of_india.no_url_for_ym", ym=ym)
            return {}

        master_path = self._cached_master_path(ym)
        if not master_path.exists():
            download_to(master_url, master_path)

        out: dict[str, str] = {}
        try:
            wb = openpyxl.load_workbook(
                master_path, read_only=True, data_only=True,
            )
        except Exception as e:  # noqa: BLE001 — surface any payload issue
            log.error(
                "holdings.bank_of_india.workbook_open_failed",
                ym=ym, path=str(master_path), err=str(e),
            )
            return {}

        try:
            if "Index" not in wb.sheetnames:
                log.warning(
                    "holdings.bank_of_india.no_index_sheet",
                    ym=ym, sheetnames=wb.sheetnames[:5],
                )
                return {}
            idx_ws = wb["Index"]
            sheet_set = set(wb.sheetnames)
            for i, row in enumerate(idx_ws.iter_rows(values_only=True)):
                if i == 0:
                    continue  # "Scheme Code | Scheme Names" header
                code = row[0] if len(row) > 0 else None
                raw_name = row[1] if len(row) > 1 else None
                if not code:
                    continue
                code_s = str(code).strip()
                if code_s not in sheet_set:
                    # Footnote / disclaimer rows in the Index column A —
                    # they don't point at a real sheet, so skip.
                    continue
                scheme_name = _extract_scheme_name(raw_name)
                if not scheme_name:
                    continue
                out.setdefault(_normalize_for_match(scheme_name), master_url)
        finally:
            wb.close()

        log.info(
            "holdings.bank_of_india.discover",
            ym=ym, n_schemes=len(out), master_url=master_url,
        )
        return out

    def _select_master_url(self, ym: str) -> str | None:
        """Query the GetDocuments AJAX endpoint and pick the consolidated
        Excel whose filename date falls in data month `ym`."""
        import httpx

        body = {
            "pagno": 0,
            "category": None,
            "fromDate": None,
            "toDate": None,
            "LibraryName": "InvestorCorner",
            "folderName": "MONTHLY PORTFOLIO",
            "CategoryValue": "no",
        }
        with httpx.Client(
            timeout=30.0,
            headers={
                "User-Agent": _UA,
                "Content-Type": "application/json;charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "*/*",
                "Referer": _INVESTOR_CORNER,
            },
            follow_redirects=True,
        ) as c:
            r = c.post(_LISTING_API, json=body)
            r.raise_for_status()
            payload = r.json()

        import json as _json

        inner = payload.get("d")
        if not inner:
            return None
        docs = _json.loads(inner).get("Documents") or []
        for doc in docs:
            url = doc.get("FolderUrl")
            if not url:
                continue
            url = _strip_query(url)
            if url.startswith("/"):
                url = _BOI_HOST + url
            filename = unquote(url.rsplit("/", 1)[-1])
            if _file_ym(filename) == ym:
                return url
        return None

    def _cached_master_path(self, ym: str) -> Path:
        return paths.holdings_excel_raw(self.amc_slug, ym, _master_filename(ym))

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Return the cached consolidated workbook for this month.

        `scheme_filename` is intentionally ignored — one workbook serves
        every scheme in this ym (same contract as the Nippon adapter).
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
        """Look up the scheme's sheet via the Index sheet, then delegate to
        the validated single-sheet SEBI parser (which auto-detects columns
        and the fraction → percent unit)."""
        wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        try:
            sheet_code = self._find_sheet_for_scheme(wb, scheme_name_printed)
            if sheet_code is None:
                log.warning(
                    "holdings.bank_of_india.scheme_not_in_index",
                    scheme=scheme_name_printed, ym=ym,
                )
                return
            recs = _parse_one_sheet(wb[sheet_code])
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

    @staticmethod
    def _find_sheet_for_scheme(
        wb: openpyxl.Workbook, scheme_name_printed: str,
    ) -> str | None:
        """Reverse-lookup the scheme's sheet code from the Index sheet.

        Match is case-insensitive on the bare scheme name (parenthesised
        description stripped). Returns the sheet code, or None if absent.
        """
        if "Index" not in wb.sheetnames:
            return None
        # scheme_name_printed comes back through discover_scheme_urls already
        # normalized (_normalize_for_match), so normalize the Index names the
        # same way before comparing — otherwise the Series-2 spelling fix would
        # break the sheet lookup (Index still spells it "Mid Cap").
        target = re.sub(
            r"\s+", " ", _normalize_for_match(scheme_name_printed).strip().lower()
        )
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
            name = _extract_scheme_name(raw_name)
            if name and _normalize_for_match(name).lower() == target:
                return code_s
        return None
