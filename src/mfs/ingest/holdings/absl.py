"""Aditya Birla Sun Life (ABSL) monthly portfolio holdings adapter.

Like Nippon (and unlike HDFC/SBI), ABSL publishes ONE consolidated workbook
per month covering every ABSLMF scheme. Two complications make this adapter
bespoke rather than a one-line ``GenericHoldingsAdapter``:

1. **Format.** The monthly disclosure is a *legacy BIFF* ``.xls`` (OLE2 /
   Composite Document), NOT an OOXML ``.xlsx`` zip. ``openpyxl`` — which the
   shared ``parse_sebi_excel`` helper uses — cannot read BIFF, so we parse
   with ``xlrd`` (xlrd >= 2.0 reads ``.xls`` only, which is exactly right).
   The workbook is usually delivered wrapped in a ``.zip`` (one ``.xls``
   inside); occasionally ABSL posts the bare ``.xls``. We handle both.

2. **Discovery.** ABSL's own portfolio/disclosures pages
   (``mutualfund.adityabirlacapital.com/forms-and-downloads/portfolio`` and
   ``/disclosures``) are Sitecore SPAs: the scheme/month listing is injected
   client-side via a ``CustomApi/Resources/FactsheetAccordionById`` AJAX call
   whose Sitecore content GUID is NOT present in the server-rendered HTML
   (only the FAQ accordions' GUIDs are), so the listing is not statically
   scrapeable and the backing call can't be reconstructed without executing
   the page JS. AdvisorKhoj, however, maintains a plain-HTML index of ABSL's
   monthly portfolio disclosures whose every anchor is the *official ABSL
   media URL* labelled ``Monthly Portfolio Disclosure - <Month> <Year>``
   (the DATA month). We scrape that index to resolve the official ABSL ZIP/
   XLS URL for the requested data month, then download from ABSL directly.

   The ABSL filename itself is inconsistent month-to-month
   (``monthly-disclosure-april-30-2026.zip`` / ``monthly-portfolio-mar-2026
   .zip`` / ``sebi_monthly_portfolio-28-feb-2026.zip`` / a bare ``.xls``), so
   we deliberately key off the AdvisorKhoj anchor *label* (always
   ``<Month> <Year>``) rather than guessing a filename — next month works
   without code changes.

Per-scheme sheet layout (validated against ABSL Flexi Cap, April 2026):
- Sheet 0 is ``Index``: col B (1) = FUND CODE (== the per-scheme sheet name),
  col C (2) = FUND NAME. Header at row 1, data from row 2.
- Sheets 1..N: one per scheme, sheet name == the index FUND CODE.
- Per-scheme layout:
    row 0: ``<CODE> | <SCHEME NAME>`` banner
    row 1: one-line scheme description
    row 2: ``Portfolio Statement as on <Month DD, YYYY>``
    row 3: header — col B='Name of the Instrument', col C='ISIN',
           col D='Industry^ / Rating', col E='Quantity',
           col F='Market/Fair Value (Rs.in Lacs)', col G='% to Net Assets',
           col H='Yield', col I='Yield to Call'.
    row 4+: section banners ('Equity & Equity related', '(a) Listed /
            awaiting listing on Stock Exchange', 'Others', 'TREPS / Reverse
            Repo', 'Net Receivables / (Payables)') interleaved with holding
            rows, then 'Sub Total' / 'Total' / 'GRAND TOTAL' footers, then
            NAV tables + derivative notes.
- Banner / footer rows carry text in col B and an empty / non-ISIN col C.

CRITICAL UNIT NOTE: ABSL stores '% to Net Assets' as a **fraction** (ICICI
Bank at 0.0584 == 5.84%), exactly like Nippon — we multiply by 100.

We hard-stop at the first ``GRAND TOTAL`` row: everything after it is a NAV
table / derivatives-exposure notes block whose cells sometimes put free text
in the ISIN column (e.g. 'Rs. 223.32', 'Nifty 500 TRI') — the strict ISIN
regex already rejects those, but the hard stop is a second guardrail.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path

import xlrd

from mfs import paths
from mfs.ingest.holdings._base import HoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import fetch_bytes
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

# AdvisorKhoj's plain-HTML index of ABSL official monthly-portfolio URLs.
# Each anchor's href is the canonical mutualfund.adityabirlacapital.com media
# URL; each anchor's label encodes the DATA month as "<Month> <Year>".
_INDEX_PAGE = (
    "https://www.advisorkhoj.com/form-download-centre/Mutual-Funds/"
    "Aditya-Birla-Sun-Life-Mutual-Fund/Monthly-Portfolio-Disclosures"
)

# Anchor → (official ABSL media URL, link text). We only accept hrefs under
# the ABSL monthly-portfolio media directory and ending .zip/.xls/.xlsx, so
# unrelated anchors on the page can never leak in.
_ANCHOR_RE = re.compile(
    r'<a[^>]+href="(?P<url>https://mutualfund\.adityabirlacapital\.com/'
    r'-/media/bsl/files/resources/monthly-portfolio/\d{4}/'
    r'[^"]+?\.(?:zip|xls|xlsx))"[^>]*>(?P<label>[^<]+)</a>',
    re.IGNORECASE,
)

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def _is_isin(s: str) -> bool:
    """Strict ISIN check: 2 letters + 9 alphanumeric + 1 check digit."""
    return bool(_ISIN_RE.match(s))


def _ym_label_tokens(ym: str) -> tuple[str, str]:
    """data_ym='2026-04' → ('april', '2026') for case-insensitive label
    matching. ABSL/AdvisorKhoj label the file by its DATA month, so we match
    the data month directly (no publish-month offset)."""
    y, m = map(int, ym.split("-"))
    return _MONTHS[m - 1].lower(), f"{y:04d}"


def _classify_section(label: str) -> str | None:
    """Map an ABSL section-banner string to an instrument_type, or None.

    ABSL banners: 'Equity & Equity related', '(a) Listed / awaiting listing
    on Stock Exchange', 'Others', 'Margin (Future and Options)', 'Cash and
    Bank', 'TREPS / Reverse Repo', 'Net Receivables / (Payables)', plus
    'Debt Instruments' / 'Money Market Instruments' / 'REIT' / 'InvIT' on
    hybrid schemes. Subtotal/total footers return None so they don't reset
    the section.
    """
    if not label:
        return None
    s = label.strip().lower()
    if s in ("sub total", "subtotal", "total", "grand total"):
        return None
    if "equity" in s and "derivative" not in s:
        return "Equity"
    if any(k in s for k in (
        "debt", "money market", "treps", "reverse repo", "triparty",
        "tri-party", "treasury", "government securities", "g-sec", "bonds",
        "debentures", "certificate of deposit", "commercial paper",
        "zero coupon", "preference shares", "non convertible",
        "securitised debt", "corporate debt",
    )):
        return "Debt"
    if "reit" in s or "invit" in s:
        return "REIT/InvIT"
    if any(k in s for k in (
        "net receivable", "net payable", "net current asset", "cash",
        "margin", "tbill", "t-bill",
    )):
        return "Cash"
    return None


def _extract_xls_bytes(payload: bytes) -> bytes:
    """Return raw BIFF .xls bytes from a download that is either a bare .xls
    or a .zip wrapping a single .xls. Raises if neither shape is found."""
    # Legacy .xls (OLE2 Composite Document) magic.
    if payload[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return payload
    if payload[:2] == b"PK":  # zip container
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            xls_names = [
                n for n in zf.namelist() if n.lower().endswith((".xls", ".xlsx"))
            ]
            if not xls_names:
                raise ValueError(
                    f"ABSL zip has no .xls/.xlsx member: {zf.namelist()}"
                )
            # ABSL ships exactly one consolidated workbook per zip.
            return zf.read(xls_names[0])
    raise ValueError(
        "ABSL payload is neither a BIFF .xls nor a .zip "
        f"(first bytes: {payload[:4]!r})"
    )


def _extract_scheme_name_from_index(raw: object) -> str | None:
    """Normalize an Index-sheet FUND NAME cell to a clean printed name."""
    if not isinstance(raw, str):
        return None
    name = re.sub(r"\s+", " ", raw).strip()
    return name or None


@register_adapter
class AbslHoldingsAdapter(HoldingsAdapter):
    amc_slug = "absl"
    source_label = "Aditya Birla Sun Life Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Resolve the official ABSL consolidated workbook URL for data month
        ``ym`` via the AdvisorKhoj index, download+extract it once, then
        enumerate every scheme from the workbook's Index sheet.

        Because ABSL ships ONE workbook for all schemes, every returned
        entry maps to the same URL (Nippon-style). ``fetch_excel`` is
        overridden to short-circuit to the already-cached extracted .xls.
        """
        url = self._resolve_master_url(ym)
        if url is None:
            log.warning("holdings.absl.no_url_for_ym", ym=ym)
            return {}

        master_path = self._ensure_master_cached(url, ym)
        out: dict[str, str] = {}
        try:
            wb = xlrd.open_workbook(master_path)
        except Exception as e:  # noqa: BLE001 — surface any payload issue
            log.error(
                "holdings.absl.workbook_open_failed",
                ym=ym, path=str(master_path), err=str(e),
            )
            return {}

        sheet_set = set(wb.sheet_names())
        if "Index" not in sheet_set:
            log.warning(
                "holdings.absl.no_index_sheet",
                ym=ym, sheetnames=wb.sheet_names()[:5],
            )
            return {}
        idx = wb.sheet_by_name("Index")
        for r in range(idx.nrows):
            code = idx.cell_value(r, 1) if idx.ncols > 1 else None
            name = idx.cell_value(r, 2) if idx.ncols > 2 else None
            code_s = str(code).strip() if code else ""
            # Skip the header row ('FUND CODE'/'FUND NAME') and any code that
            # doesn't correspond to an actual scheme sheet.
            if not code_s or code_s not in sheet_set:
                continue
            scheme = _extract_scheme_name_from_index(name)
            if not scheme:
                continue
            out.setdefault(scheme, url)

        log.info("holdings.absl.discover", ym=ym, n_schemes=len(out), url=url)
        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_master_url(self, ym: str) -> str | None:
        """Scrape the AdvisorKhoj index; return the official ABSL URL whose
        anchor label matches the requested data month, else None."""
        html = fetch_bytes(_INDEX_PAGE).decode("utf-8", errors="replace")
        month_lc, year = _ym_label_tokens(ym)
        for m in _ANCHOR_RE.finditer(html):
            label = re.sub(r"\s+", " ", m.group("label")).strip().lower()
            # Label is e.g. "monthly portfolio disclosure - april 2026".
            if month_lc in label and year in label:
                return m.group("url")
        return None

    def _cached_master_path(self, ym: str) -> Path:
        """Local cache path for the extracted consolidated .xls. We always
        store the BIFF .xls (extracted from the zip if necessary)."""
        return paths.holdings_excel_raw(
            self.amc_slug, ym, f"absl-monthly-portfolio-{ym}.xls"
        )

    def _ensure_master_cached(self, url: str, ym: str) -> Path:
        """Download the ABSL payload (zip or xls) and cache the inner BIFF
        .xls locally. Idempotent."""
        out = self._cached_master_path(ym)
        if out.exists():
            return out
        payload = fetch_bytes(url)
        xls = _extract_xls_bytes(payload)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(xls)
        tmp.rename(out)
        return out

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Return the cached extracted consolidated .xls for this month.

        ABSL's workbook is shared across all schemes in this ym, so
        ``scheme_filename`` is intentionally ignored. We still download +
        extract here (idempotent) so a caller that invokes fetch without
        going through discover first still works.
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
        ISIN-bearing holding rows (weights converted from fraction to %)."""
        wb = xlrd.open_workbook(excel_path)
        sheet_code = self._find_sheet_for_scheme(wb, scheme_name_printed)
        if sheet_code is None:
            log.warning(
                "holdings.absl.scheme_not_in_index",
                scheme=scheme_name_printed, ym=ym,
            )
            return
        ws = wb.sheet_by_name(sheet_code)

        current_section = "Equity"  # safe default if banner missing
        seen: set[str] = set()
        for r in range(ws.nrows):
            name = ws.cell_value(r, 1) if ws.ncols > 1 else None
            isin = ws.cell_value(r, 2) if ws.ncols > 2 else None
            weight = ws.cell_value(r, 6) if ws.ncols > 6 else None

            name_s = name.strip() if isinstance(name, str) else ""
            isin_s = str(isin).strip() if isin is not None else ""

            # Hard stop at GRAND TOTAL — everything after is NAV tables /
            # derivatives notes whose cells sometimes carry free text.
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

            # ABSL stores % to Net Assets as a fraction (0.0584 == 5.84%).
            weight_pct = w * 100.0

            if isin_s in seen:
                continue
            seen.add(isin_s)

            yield ParsedHoldingRecord(
                scheme_name_printed=scheme_name_printed,
                security_name=name_s.rstrip(" *"),
                weight_pct=weight_pct,
                isin=isin_s,
                instrument_type=current_section,
                source_amc=self.amc_slug,
            )

    @staticmethod
    def _find_sheet_for_scheme(
        wb: xlrd.book.Book, scheme_name_printed: str
    ) -> str | None:
        """Reverse-lookup the per-scheme sheet code (== Index FUND CODE) from
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
            clean = _extract_scheme_name_from_index(name)
            if clean and clean.lower() == target:
                return code_s
        return None
