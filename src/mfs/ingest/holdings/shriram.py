"""Shriram Mutual Fund monthly portfolio holdings adapter.

Like Nippon / ABSL (and unlike HDFC/SBI), Shriram publishes ONE consolidated
workbook per month covering every Shriram scheme, so this adapter is bespoke
rather than a one-line ``GenericHoldingsAdapter``.

1. **Format.** The monthly disclosure is a *legacy BIFF* ``.xls`` (OLE2 /
   Composite Document, ``file(1)`` → "Composite Document File V2 Document"),
   NOT an OOXML ``.xlsx`` zip — so ``openpyxl`` (which the shared
   ``parse_sebi_excel`` helper uses) cannot read it. We parse with ``xlrd``
   (xlrd >= 2.0 reads ``.xls`` only, which is exactly right), modelled on the
   ABSL adapter.

2. **Discovery.** Shriram's statutory-disclosures page
   (``shriramamc.in/investor-statutory-disclosures``) is a JS SPA, but the
   server-rendered HTML already embeds every portfolio Excel as a plain
   absolute ``cdn.shriramamc.in`` URL — so a single page scrape gives us the
   full catalogue without executing page JS (HDFC-style static scrape).

   The monthly-portfolio URLs live under::

       https://cdn.shriramamc.in/uploads/Statutory-disclosure/
         Monthly--Fortnightly--Weekly-Portfolio-of-Scheme(s)/
         Monthly-Portfolio-for-the-Financial-Year/<FY>/
         Monthly-Portfolio-Shriram-Mutual-Fund-<Month>-<YYYY>.xls

   where ``<FY>`` is the Indian financial year folder (``2026-2027`` for an
   April-2026 data month — Indian FY runs April→March), and ``<Month>`` is
   whatever the web team typed: a MIX of full names ("April", "February",
   "March") and abbreviations ("Jan", "Aug", "Sep", "Oct", "Dec"). Rather
   than guess a filename, we regex every monthly-portfolio URL on the page,
   parse each filename's ``<Month>-<YYYY>`` back to a data ``ym``, and pick
   the one matching the requested month — so next month works without code
   changes regardless of which month spelling Shriram uses.

Per-scheme sheet layout (validated against Shriram Flexi Cap, April 2026):
- One sheet PER scheme; the sheet NAME is the printed scheme name (there is
  no Index sheet). Excel's 31-char sheet-name cap truncates a couple of long
  names (e.g. "Shriram Multi Asset Allocation").
- Within a sheet:
    rows 0-17: scheme banner, riskometer blurb, then
               "Portfolio Statement as on April 30, 2026".
    row 18: header — col B(1)='Name of Instrument', col C(2)='ISIN',
            col D(3)='Industry/Rating', col E(4)='Quantity',
            col F(5)='Market/Fair Value (INR Lacs)', col G(6)='% to Net
            Assets', col H(7)='% Yield'.
    row 19+: section banners ('Equity & Equity related', '(a)Listed /
             Awaiting listing on Stock Exchanges', 'Derivative', 'Debt
             Instruments', 'Treps', 'Net Receivables / (Payables)')
             interleaved with holding rows, then 'Sub Total' / 'TOTAL' /
             'GRAND TOTAL' footers, then NAV tables + derivatives-disclosure
             + sector-classification blocks.
- Banner / footer rows carry text in col B and an empty / non-ISIN col C.
- Derivative rows (futures / written options) carry no ISIN in col C, so the
  strict ISIN regex rejects them and they never reach the output.

UNIT NOTE: Shriram stores '% to Net Assets' as an actual **percent** (HDFC
Bank at 7.72 == 7.72%, GRAND TOTAL == 100.0), NOT a fraction like
ABSL/Nippon — so we do NOT multiply by 100.

We hard-stop at the first ``GRAND TOTAL`` row: everything after it is NAV /
derivatives-disclosure / sector tables whose cells sometimes carry stray
weights and free text in non-ISIN columns. The strict ISIN regex already
rejects those, but the hard stop is a second guardrail.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote

import xlrd

from mfs import paths
from mfs.ingest.holdings._base import HoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import fetch_bytes
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = "https://www.shriramamc.in/investor-statutory-disclosures"
_CDN_HOST = "https://cdn.shriramamc.in"

# Month tokens that appear in filenames. Order matters within a list: try the
# long form before the short form so "April" doesn't fall through to "Apr".
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

# Matches a Shriram monthly-portfolio Excel URL embedded in the page HTML.
# We anchor on the "Monthly-Portfolio-for-the-Financial-Year" directory and
# the "Monthly-Portfolio-Shriram-Mutual-Fund-<Month>-<YYYY>" filename so we
# never collide with the Fortnightly / Weekly / ETF-additional-disclosure
# files that live under sibling directories. The month name (full or
# abbreviated) and 4-digit year are captured to verify the data month.
#
# The path component "Monthly--Fortnightly--Weekly-Portfolio-of-Scheme(s)"
# contains literal parentheses; we match the whole path loosely up to the
# financial-year directory rather than hard-coding the punctuation.
_URL_RE = re.compile(
    r"(https://cdn\.shriramamc\.in/[^\"'\s]*?"
    r"Monthly-Portfolio-for-the-Financial-Year/\d{4}-\d{4}/"
    r"Monthly-Portfolio-Shriram-Mutual-Fund-"
    r"(?P<month>[A-Za-z]+)-(?P<year>\d{4})\.xls)",
    re.IGNORECASE,
)

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")

# Sheet-level column layout (0-indexed): name=col B(1), ISIN=col C(2),
# % to Net Assets = col G(6).
_NAME_COL = 1
_ISIN_COL = 2
_WEIGHT_COL = 6


def _is_isin(s: str) -> bool:
    """Strict ISIN check: 2 letters + 9 alphanumeric + 1 check digit."""
    return bool(_ISIN_RE.match(s))


def _month_name_to_int(name: str) -> int | None:
    """Map an English month name (full or abbreviated) to 1..12, else None."""
    n = name.strip().lower()
    for month_int, tokens in _MONTH_TOKENS.items():
        for t in tokens:
            if n == t.lower():
                return month_int
    return None


def _parse_filename_ym(filename: str) -> str | None:
    """Reverse-derive data_ym (YYYY-MM) from a monthly-portfolio filename.

    Returns None if the filename's month/year don't parse. The DATA month is
    encoded directly in the filename (no publish-month offset).
    """
    m = re.search(
        r"Monthly-Portfolio-Shriram-Mutual-Fund-"
        r"(?P<month>[A-Za-z]+)-(?P<year>\d{4})\.xls",
        filename,
        re.IGNORECASE,
    )
    if not m:
        return None
    month = _month_name_to_int(m.group("month"))
    if month is None:
        return None
    return f"{int(m.group('year')):04d}-{month:02d}"


def _classify_section(label: str) -> str | None:
    """Map a Shriram section-banner string to an instrument_type, or None.

    Shriram banners: 'Equity & Equity related', '(a)Listed / Awaiting listing
    on Stock Exchanges', 'Derivative' / 'Derivatives (Index / Stock Futures)'
    / 'Index / Stock Option', 'Debt Instruments', 'Treps', 'Net Receivables /
    (Payables)', plus REIT/InvIT on hybrid schemes. Subtotal/total/grand-total
    footers return None so they don't reset the section. 'Derivative*' returns
    None too (its rows carry no ISIN, so they're dropped anyway, and we keep
    the prior Equity section for any stray ISIN-bearing row).
    """
    if not label:
        return None
    s = label.strip().lower()
    if s in ("sub total", "subtotal", "total", "grand total"):
        return None
    if "equity" in s and "derivative" not in s:
        return "Equity"
    if any(
        k in s
        for k in (
            "debt", "money market", "treps", "reverse repo", "triparty",
            "tri-party", "treasury", "government securities", "g-sec",
            "bonds", "debentures", "certificate of deposit",
            "commercial paper", "zero coupon", "preference shares",
            "non convertible", "securitised debt", "corporate debt",
        )
    ):
        return "Debt"
    if "reit" in s or "invit" in s:
        return "REIT/InvIT"
    if any(
        k in s
        for k in (
            "net receivable", "net payable", "net current asset", "cash",
            "tbill", "t-bill",
        )
    ):
        return "Cash"
    return None


@register_adapter
class ShriramHoldingsAdapter(HoldingsAdapter):
    amc_slug = "shriram"
    source_label = "Shriram Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Resolve the consolidated workbook URL for data month ``ym`` by
        scraping the disclosures page, download it once, then enumerate every
        scheme from the workbook's sheet names.

        Because Shriram ships ONE workbook for all schemes, every returned
        entry maps to the same URL (Nippon/ABSL-style). ``fetch_excel`` is
        overridden to short-circuit to the already-cached .xls.
        """
        url = self._resolve_master_url(ym)
        if url is None:
            log.warning("holdings.shriram.no_url_for_ym", ym=ym)
            return {}

        master_path = self._cached_master_path(ym)
        if not master_path.exists():
            self._download_master(url, master_path)

        try:
            wb = xlrd.open_workbook(master_path)
        except Exception as e:  # noqa: BLE001 — surface any payload issue
            log.error(
                "holdings.shriram.workbook_open_failed",
                ym=ym, path=str(master_path), err=str(e),
            )
            return {}

        out: dict[str, str] = {}
        for sheet_name in wb.sheet_names():
            ws = wb.sheet_by_name(sheet_name)
            if not self._is_portfolio_sheet(ws):
                continue
            scheme = re.sub(r"\s+", " ", sheet_name).strip()
            if scheme:
                out.setdefault(scheme, url)

        log.info("holdings.shriram.discover", ym=ym, n_schemes=len(out), url=url)
        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_master_url(self, ym: str) -> str | None:
        """Scrape the disclosures page; return the monthly-portfolio URL whose
        filename date matches the requested data month, else None."""
        html = fetch_bytes(_DISCLOSURE_PAGE).decode("utf-8", errors="replace")
        for m in _URL_RE.finditer(html):
            url = m.group(1)
            filename = unquote(url.rsplit("/", 1)[-1])
            if _parse_filename_ym(filename) == ym:
                return url
        return None

    @staticmethod
    def _is_portfolio_sheet(ws: xlrd.sheet.Sheet) -> bool:
        """True if a sheet looks like a scheme portfolio table (has an ISIN
        header in the expected column). Guards against any stray non-portfolio
        sheet sneaking into the scheme list."""
        for r in range(min(40, ws.nrows)):
            cell = ws.cell_value(r, _ISIN_COL) if ws.ncols > _ISIN_COL else None
            if isinstance(cell, str) and cell.strip().upper() == "ISIN":
                return True
        return False

    def _cached_master_path(self, ym: str) -> Path:
        """Local cache path for the consolidated .xls (kept as BIFF .xls)."""
        return paths.holdings_excel_raw(
            self.amc_slug, ym, f"shriram-monthly-portfolio-{ym}.xls"
        )

    def _download_master(self, url: str, out: Path) -> Path:
        """Download the consolidated .xls and cache it locally. Idempotent.

        We verify the OLE2 (Composite Document) magic so a stray HTML error
        page can never be cached as a workbook.
        """
        payload = fetch_bytes(url)
        if payload[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise ValueError(
                f"Shriram payload is not a BIFF .xls (first bytes: "
                f"{payload[:4]!r}) from {url}"
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.rename(out)
        return out

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Return the cached consolidated .xls for this month.

        Shriram's workbook is shared across all schemes in this ym, so
        ``scheme_filename`` is intentionally ignored. We still download here
        (idempotent) so a caller that invokes fetch without going through
        discover first still works.
        """
        out = self._cached_master_path(ym)
        if out.exists():
            return out
        return self._download_master(url, out)

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def parse_excel(
        self,
        excel_path: Path,
        scheme_name_printed: str,
        ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        """Walk the scheme's own sheet inside the consolidated workbook and
        yield ISIN-bearing holding rows (weights are already percent)."""
        wb = xlrd.open_workbook(excel_path)
        sheet_name = self._find_sheet_for_scheme(wb, scheme_name_printed)
        if sheet_name is None:
            log.warning(
                "holdings.shriram.scheme_not_found",
                scheme=scheme_name_printed, ym=ym,
            )
            return
        ws = wb.sheet_by_name(sheet_name)

        current_section = "Equity"  # safe default if a banner is missing
        seen: set[str] = set()
        for r in range(ws.nrows):
            name = ws.cell_value(r, _NAME_COL) if ws.ncols > _NAME_COL else None
            isin = ws.cell_value(r, _ISIN_COL) if ws.ncols > _ISIN_COL else None
            weight = (
                ws.cell_value(r, _WEIGHT_COL) if ws.ncols > _WEIGHT_COL else None
            )

            name_s = name.strip() if isinstance(name, str) else ""
            isin_s = str(isin).strip() if isin is not None else ""

            # Hard stop at GRAND TOTAL — everything after is NAV / derivatives
            # / sector tables whose cells sometimes carry stray weights.
            if name_s.lower() == "grand total":
                break

            # Section banner / footer row: name present, col C not an ISIN.
            if name_s and not _is_isin(isin_s):
                section = _classify_section(name_s)
                if section:
                    current_section = section
                continue

            if not _is_isin(isin_s) or not name_s:
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
                security_name=name_s.rstrip(" *#^"),
                weight_pct=w,
                isin=isin_s,
                instrument_type=current_section,
                source_amc=self.amc_slug,
            )

    @staticmethod
    def _find_sheet_for_scheme(
        wb: xlrd.book.Book, scheme_name_printed: str
    ) -> str | None:
        """Reverse-lookup the per-scheme sheet whose (whitespace-normalized)
        name matches the requested printed scheme name, case-insensitively."""
        target = re.sub(r"\s+", " ", scheme_name_printed.strip()).lower()
        for sheet_name in wb.sheet_names():
            if re.sub(r"\s+", " ", sheet_name.strip()).lower() == target:
                return sheet_name
        return None
