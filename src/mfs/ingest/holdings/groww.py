"""Groww Mutual Fund monthly portfolio holdings adapter.

Like Nippon (and unlike HDFC's one-Excel-per-scheme), Groww publishes ONE
consolidated workbook per month covering every Groww scheme:

    https://assets-netstorage.growwmf.in/compliance_docs/Statutory Disclosure/
        Portfolio/<FY-folder>/Monthly Portfolio- <Mon> <DD>, <YYYY>.xlsx

The statutory-disclosure portfolio page is server-rendered: its HTML embeds
every Excel link (monthly + fortnightly, all years) as a plain percent-
encoded absolute URL on the `assets-netstorage.growwmf.in` host, so a single
page scrape gives the full catalog. We pick the MONTHLY file whose filename
date falls in the requested data month.

Filename naming is human-entered and inconsistent across months, so we match
on the month-name token + 4-digit year only and ignore everything else:
- month: full or abbreviated ("Apr"/"April", "Jul"/"July", "Sep"/"Sept", …)
- day component: present/absent/comma'd ("- Apr 30, 2026", "- Mar 31 2024",
  "- Oct 2023", "- January 31, 2025") — irrelevant to the data month
- FY folder: "2026 -2027" / "2025-2026" / "2024- 2025" — also irrelevant
- extension: .xls or .xlsx (the .xls files are still OOXML zips; we cache
  every payload under .xlsx so openpyxl accepts the suffix)
Note the historical typo "Montlhy Portfolio-" — we anchor the regex on
"Mon...hly Portfolio" loosely enough to catch the data month we care about
while excluding the Fortnightly files.

Workbook structure (validated against April 2026 file, ~2.6 MB, 57 schemes):
- One sheet per scheme, sheet names are opaque 2-char codes ('BC', 'EH', …);
  there is NO Index sheet. The scheme name lives in row 0, col B, prefixed
  with an internal code, e.g. 'IB01-Groww Large Cap Fund'. We strip the
  'IB\\d+-' prefix to recover the printed scheme name.
- A trailing 'XDO_METADATA' sheet carries BI-Publisher template metadata,
  not a portfolio — it has no 'IB\\d+-' banner so it's skipped automatically.
- Per-scheme layout is the standard SEBI table: ISIN col B, Name col C,
  Rating/Industry col D, Quantity col E, Market Value col F, '% To Net
  Assets' col G (stored as a FRACTION, e.g. 0.0817 = 8.17%), with
  'EQUITY & EQUITY RELATED' / 'DEBT INSTRUMENTS' / 'MONEY MARKET …' /
  section banners interleaved.

Because the layout is the generic SEBI shape, the per-scheme parse delegates
to ``parse_sebi_excel`` (which auto-detects the header row, ISIN/name/weight
columns, and the percent-vs-fraction unit) on the matched sheet — we only
add the consolidated-workbook plumbing modeled on the Nippon adapter:
discovery enumerates every per-scheme banner (all pointing at the same
master URL), and ``parse_excel`` resolves the printed name back to its sheet.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote

import openpyxl

from mfs import paths
from mfs.ingest.holdings._generic import GenericHoldingsAdapter, parse_sebi_excel
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import download_to, fetch_bytes
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = "https://www.growwmf.in/statutory-disclosure/portfolio"
_FILES_HOST = "assets-netstorage.growwmf.in"

# Month-name tokens that appear in Groww filenames. Long form first so that
# e.g. "April" is preferred over "Apr" when both could match. "Sept" is an
# observed extra variant on some AMC files; included for forward safety.
_MONTH_TOKENS: dict[int, list[str]] = {
    1: ["January", "Jan"],
    2: ["February", "Feb"],
    3: ["March", "Mar"],
    4: ["April", "Apr"],
    5: ["May"],
    6: ["June", "Jun"],
    7: ["July", "Jul"],
    8: ["August", "Aug"],
    9: ["September", "Sept", "Sep"],
    10: ["October", "Oct"],
    11: ["November", "Nov"],
    12: ["December", "Dec"],
}

# Matches a percent-encoded MONTHLY portfolio Excel URL on the Groww assets
# host. We deliberately:
#   - anchor on the assets host + .../Portfolio/ path so we don't pick up
#     AAUM or other disclosure files,
#   - require "Mon...hly%20Portfolio" (NOT "Fortnightly") to exclude the
#     fortnightly files while tolerating the historical "Montlhy" typo,
#   - keep the URL match tight (no '"' or whitespace) and parse the decoded
#     filename downstream to extract the data month.
_URL_RE = re.compile(
    r'(https://assets-netstorage\.growwmf\.in/compliance_docs/'
    r'[^"]*?/Portfolio/[^"]*?/Mon[a-z]*hly%20Portfolio[^"]*?\.xlsx?)',
    re.IGNORECASE,
)

# Strip the internal "IB<digits>-" code that prefixes each sheet's scheme
# banner (e.g. "IB01-Groww Large Cap Fund" -> "Groww Large Cap Fund").
_SCHEME_BANNER_RE = re.compile(r"^\s*IB\d+\s*-\s*(?P<name>.+?)\s*$", re.IGNORECASE)

# Workbook-vs-AMFI spelling reconciliation. Groww's workbook banner prints
# "Groww Large Cap Fund" (two tokens) but AMFI / scheme_master records the
# renamed scheme as "Groww Largecap Fund (formerly known as Indiabulls Blue
# Chip Fund)" (one token, "Largecap"). The shared fuzzy matcher scores on
# token_set_ratio, where "LARGE"+"CAP" vs "LARGECAP" drops the score to ~82
# — below the 85 acceptance threshold — so the only ranked Groww equity fund
# affected goes unmatched. The Levenshtein distance is a single space, but
# that scorer is only the tie-break, not primary. We close the gap by
# emitting the AMFI token spelling ("Largecap") for the printed name, which
# is also what parse_excel uses to re-locate the sheet (it normalizes the
# banner the same way), so discovery and sheet-lookup stay consistent.
_BANNER_TOKEN_FIXUPS = ((re.compile(r"\bLarge\s+Cap\b", re.IGNORECASE), "Largecap"),)

_METADATA_SHEET = "XDO_METADATA"

# Filename -> data month. Matches the FIRST month token + the 4-digit year
# anywhere after "Portfolio". The day (if any) is intentionally ignored.
_FNAME_MONTH_RE = re.compile(
    r"\bMon[a-z]*hly\s+Portfolio\b.*?"
    r"(?P<month>[A-Za-z]+)\s+(?:\d{1,2}\s*,?\s*)?(?P<year>\d{4})",
    re.IGNORECASE,
)


def _month_name_to_int(name: str) -> int | None:
    n = name.strip().lower()
    for month_int, tokens in _MONTH_TOKENS.items():
        for t in tokens:
            if n == t.lower():
                return month_int
    return None


def _filename_data_ym(filename: str) -> str | None:
    """Reverse-derive data_ym (YYYY-MM) from a Groww monthly filename, or
    None if it doesn't parse to a recognizable month/year."""
    m = _FNAME_MONTH_RE.search(filename)
    if not m:
        return None
    month = _month_name_to_int(m.group("month"))
    if month is None:
        return None
    year = int(m.group("year"))
    return f"{year:04d}-{month:02d}"


def _master_excel_filename(ym: str) -> str:
    """Canonical local-cache filename for the consolidated monthly workbook.

    Groww ships ONE workbook for all schemes per month; some months arrive
    with a .xls extension despite being OOXML, so we always cache as .xlsx
    so openpyxl accepts the suffix.
    """
    return f"Groww-Monthly-Portfolio-{ym}.xlsx"


def _printed_scheme_name(banner: str | None) -> str | None:
    """Recover the printed scheme name from a sheet's row-0 col-B banner."""
    if not isinstance(banner, str):
        return None
    m = _SCHEME_BANNER_RE.match(banner)
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group("name")).strip()
    for pat, repl in _BANNER_TOKEN_FIXUPS:
        name = pat.sub(repl, name)
    return name


def _sheet_banner(ws) -> str | None:
    """Return the first non-empty string cell in the sheet's first row."""
    first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
    if not first:
        return None
    for cell in first:
        if isinstance(cell, str) and cell.strip():
            return cell.strip()
    return None


@register_adapter
class GrowwHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "groww"
    source_label = "Groww Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Find the consolidated monthly workbook for ``ym``, cache it once,
        and enumerate the per-scheme banners from its sheets.

        Every scheme maps to the SAME master URL (consolidated workbook);
        ``parse_excel`` later resolves the printed name back to its sheet.
        """
        html = fetch_bytes(_DISCLOSURE_PAGE).decode("utf-8", errors="replace")
        master_url = self._select_master_url(html, ym)
        if master_url is None:
            log.warning("holdings.groww.no_url_for_ym", ym=ym)
            return {}

        master_path = self._cached_master_path(ym)
        if not master_path.exists():
            download_to(master_url, master_path)

        try:
            wb = openpyxl.load_workbook(
                master_path, read_only=True, data_only=True,
            )
        except Exception as e:  # noqa: BLE001 — surface any payload issue
            log.error(
                "holdings.groww.workbook_open_failed",
                ym=ym, path=str(master_path), err=str(e),
            )
            return {}

        out: dict[str, str] = {}
        try:
            for code in wb.sheetnames:
                if code == _METADATA_SHEET:
                    continue
                name = _printed_scheme_name(_sheet_banner(wb[code]))
                if not name:
                    continue
                out.setdefault(name, master_url)
        finally:
            wb.close()

        log.info(
            "holdings.groww.discover",
            ym=ym, n_schemes=len(out), master_url=master_url,
        )
        return out

    def _select_master_url(self, html: str, ym: str) -> str | None:
        """Pick the monthly URL whose decoded filename date matches ``ym``."""
        for m in _URL_RE.finditer(html):
            url = m.group(1)
            filename = unquote(url.rsplit("/", 1)[-1])
            if _filename_data_ym(filename) == ym:
                return url
        return None

    def _cached_master_path(self, ym: str) -> Path:
        return paths.holdings_excel_raw(
            self.amc_slug, ym, _master_excel_filename(ym),
        )

    # ------------------------------------------------------------------
    # Download — every scheme shares the one consolidated workbook.
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        master_path = self._cached_master_path(ym)
        if master_path.exists():
            return master_path
        return download_to(url, master_path)

    # ------------------------------------------------------------------
    # Parse — locate the scheme's sheet, then reuse the generic SEBI parser.
    # ------------------------------------------------------------------

    def parse_excel(
        self,
        excel_path: Path,
        scheme_name_printed: str,
        ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        sheet_index = self._sheet_index_for_scheme(excel_path, scheme_name_printed)
        if sheet_index is None:
            log.warning(
                "holdings.groww.scheme_sheet_not_found",
                scheme=scheme_name_printed, ym=ym,
            )
            return iter(())
        return parse_sebi_excel(
            excel_path,
            scheme_name_printed,
            self.amc_slug,
            sheet_index=sheet_index,
            expect_ym=ym,
        )

    @staticmethod
    def _sheet_index_for_scheme(
        excel_path: Path, scheme_name_printed: str,
    ) -> int | None:
        """Map a printed scheme name to its sheet index by matching the
        'IB\\d+-<name>' banner in each sheet's first row."""
        target = re.sub(r"\s+", " ", scheme_name_printed.strip()).lower()
        wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        try:
            for idx, code in enumerate(wb.sheetnames):
                if code == _METADATA_SHEET:
                    continue
                name = _printed_scheme_name(_sheet_banner(wb[code]))
                if name and name.lower() == target:
                    return idx
        finally:
            wb.close()
        return None
