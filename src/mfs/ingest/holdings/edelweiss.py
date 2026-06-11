"""Edelweiss Mutual Fund monthly portfolio holdings adapter (Phase 5).

Like Nippon, Edelweiss publishes ONE consolidated workbook per month covering
every scheme:

    https://www.edelweissmf.com/Files/MONTHLY PORTFOLIO OF SCHEME/
      Monthly Portfolio and Risk-o-Meter/{YYYY}/{Mon}/{published}/
      EDEL_Portfolio Monthly Notes {DD}-{Mon}-{YYYY}_{timestamp}.xlsx

…where {Mon} is the 3-letter English month abbreviation, {DD}-{Mon}-{YYYY} is
the data month-end date, and {timestamp} is the upload datetime
(`ddmmyyyy_hhmmss_AM|PM`). The `{published}` path segment is lower- or
upper-cased depending on the month Edelweiss's web team uploaded it
("published" vs "Published").

DISCOVERY — why we scrape AdvisorKhoj rather than the AMC site
--------------------------------------------------------------
edelweissmf.com is an Angular SPA fronted by Akamai bot protection. The
month-keyed portfolio listing is served only by a SIGNED + AES-ENCRYPTED API
(`https://api.edelweissmf.com/.../third-party/getStatutoryMenu`, request body
encrypted with `HmacSHA256(secret + clientIp + ts, hashKey)` and response
AES-encrypted). The Akamai edge returns 403 to every non-browser client even
before the app layer, so we cannot reach the listing API programmatically.

The portfolio FILES themselves live on the same host under `/Files/...` and
ARE fetchable once we send a complete Chrome header fingerprint (UA +
`sec-ch-ua*` client hints + `Sec-Fetch-*`). The only missing piece is the
non-deterministic `{timestamp}` in each filename — which we cannot guess.

AdvisorKhoj's form-download centre mirrors the canonical edelweissmf.com
`/Files/...` URLs for every month of history (it links straight to the AMC's
own CDN, not a re-hosted copy). We scrape that page to RESOLVE the timestamped
URL for the requested data month, then DOWNLOAD the file from Edelweiss's own
CDN — so the bytes are sourced from the AMC, AdvisorKhoj only supplies the URL.

Workbook structure (validated against April 2026 file, 72 sheets):
- Sheet 0 = "Index": header on row 2; col A = Fund Id (== sheet name code),
  col B = Fund Desc (full scheme name). Data from row 3.
- Sheets 1..N: one per scheme, sheet name == the Fund Id code.
- Per-scheme layout (standard SEBI):
    row 0: "PORTFOLIO STATEMENT OF EDELWEISS ..." banner
    row 1: scheme-type sub-banner
    row 3: header — Name of the Instrument | ISIN | Rating/Industry |
           Quantity | Market/Fair Value(Rs. In Lacs) | % to Net Assets | YIELD
    row 4+: section banners ("Equity & Equity related",
            "(a)Listed / Awaiting listing on Stock Exchanges", …) interleaved
            with holding rows.
- "% to Net Assets" is stored as a FRACTION (0.0523 = 5.23%).

We delegate the actual sheet parsing to the shared
`mfs.ingest.holdings._generic._parse_one_sheet`, which auto-detects the
header row, the ISIN/name/weight columns, the section banners, AND the
fraction-vs-percent weight unit — so this adapter only has to (a) discover
the monthly URL and (b) pick the right per-scheme sheet out of the
consolidated workbook (Nippon model).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote

import httpx
import openpyxl

from mfs import paths
from mfs.ingest.holdings._generic import GenericHoldingsAdapter, _parse_one_sheet
from mfs.ingest.holdings._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

# AdvisorKhoj mirrors the canonical edelweissmf.com /Files/ URLs for every
# month — used purely to RESOLVE the timestamped filename. The bytes are then
# pulled from Edelweiss's own CDN.
_DISCLOSURE_PAGE = (
    "https://www.advisorkhoj.com/form-download-centre/Mutual/"
    "Edelweiss-Mutual-Fund/Monthly-Portfolio-Disclosures"
)

# Edelweiss's CDN sits behind Akamai bot protection; a complete Chrome
# fingerprint (UA + client hints + Sec-Fetch-*) is required or it 403s.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": (
        '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_MONTH_ABBR = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}
_MONTH_NAME_TO_INT = {v.lower(): k for k, v in _MONTH_ABBR.items()}

# Matches a canonical Edelweiss monthly-portfolio Excel URL on the disclosures
# page. The filename's `{DD}-{Mon}-{YYYY}` is the DATA month-end date (what we
# key on); the trailing `_{timestamp}` is the upload datetime and varies. URLs
# are %20-encoded for spaces. The `published`/`Published` segment is
# case-insensitive (Edelweiss is inconsistent month to month).
_URL_RE = re.compile(
    r"https://www\.edelweissmf\.com/Files/MONTHLY%20PORTFOLIO%20OF%20SCHEME/"
    r"Monthly%20Portfolio%20and%20Risk-o-Meter/"
    r"\d{4}/[A-Za-z]{3}/[Pp]ublished/"
    r"EDEL_Portfolio%20Monthly%20Notes%20"
    r"(?P<day>\d{1,2})-(?P<mon>[A-Za-z]{3})-(?P<year>\d{4})_"
    r"[^\"'\s<>]+?\.xlsx",
    re.IGNORECASE,
)


def _filename_ym(day: str, mon: str, year: str) -> str | None:
    """Derive data_ym (YYYY-MM) from the month-end date in a filename."""
    month = _MONTH_NAME_TO_INT.get(mon.lower())
    if month is None:
        return None
    return f"{int(year):04d}-{month:02d}"


def _master_excel_filename(ym: str) -> str:
    """Local cache filename for the consolidated monthly workbook."""
    return f"EDEL-MONTHLY-PORTFOLIO-{ym}.xlsx"


def _scheme_name_from_index_cell(raw: object) -> str | None:
    """Clean the Index-sheet Fund Desc cell into a printed scheme name."""
    if not isinstance(raw, str):
        return None
    name = re.sub(r"\s+", " ", raw).strip()
    return name or None


@register_adapter
class EdelweissHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "edelweiss"
    source_label = "Edelweiss Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Resolve the consolidated workbook URL for `ym`, download it once,
        and enumerate every scheme from its Index sheet.

        Because Edelweiss ships ONE workbook for all schemes, every returned
        entry maps to the same master URL (Nippon model). `fetch_excel`
        short-circuits to the already-cached master file.
        """
        master_url = self._resolve_master_url(ym)
        if master_url is None:
            log.warning("holdings.edelweiss.no_url_for_ym", ym=ym)
            return {}

        master_path = self._cached_master_path(ym)
        if not master_path.exists():
            self._download_with_browser_headers(master_url, master_path)

        out: dict[str, str] = {}
        try:
            wb = openpyxl.load_workbook(
                master_path, read_only=True, data_only=True
            )
        except Exception as e:  # noqa: BLE001 — surface any payload issue
            log.error(
                "holdings.edelweiss.workbook_open_failed",
                ym=ym, path=str(master_path), err=str(e),
            )
            return {}

        try:
            if "Index" not in wb.sheetnames:
                log.warning(
                    "holdings.edelweiss.no_index_sheet",
                    ym=ym, sheetnames=wb.sheetnames[:5],
                )
                return {}
            idx_ws = wb["Index"]
            sheet_set = set(wb.sheetnames)
            for row in idx_ws.iter_rows(values_only=True):
                code = row[0] if len(row) > 0 else None
                raw_name = row[1] if len(row) > 1 else None
                if not code:
                    continue
                code_s = str(code).strip()
                # Index header / banner rows ("Fund Id", "EDELWEISS MUTUAL
                # FUND", …) won't match a real sheet code.
                if code_s not in sheet_set:
                    continue
                scheme_name = _scheme_name_from_index_cell(raw_name)
                if not scheme_name:
                    continue
                out.setdefault(scheme_name, master_url)
        finally:
            wb.close()

        log.info(
            "holdings.edelweiss.discover",
            ym=ym, n_schemes=len(out), master_url=master_url,
        )
        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_master_url(self, ym: str) -> str | None:
        """Scrape the disclosures page; return the URL whose filename
        month-end date falls in data month `ym`."""
        with httpx.Client(
            timeout=60.0, headers=_BROWSER_HEADERS, follow_redirects=True
        ) as c:
            r = c.get(_DISCLOSURE_PAGE)
            r.raise_for_status()
            html = r.text
        for m in _URL_RE.finditer(html):
            if _filename_ym(m.group("day"), m.group("mon"), m.group("year")) == ym:
                return unquote(m.group(0)) if "%" not in m.group(0) else m.group(0)
        return None

    def _cached_master_path(self, ym: str) -> Path:
        return paths.holdings_excel_raw(
            self.amc_slug, ym, _master_excel_filename(ym)
        )

    def _download_with_browser_headers(self, url: str, out_path: Path) -> Path:
        """Fetch the workbook from Edelweiss's CDN via the system ``curl``.

        Akamai fingerprints the TLS ClientHello + HTTP/2 frames, not just
        headers: httpx (Python/OpenSSL) is 403'd even with a complete browser
        header set, while system ``curl`` (SecureTransport/HTTP-2) with the
        same headers passes. So we shell out to ``curl``; a non-2xx status or
        a non-OOXML body raises, keeping the fail-fast invariant (we never
        cache a 403 HTML error page as if it were the workbook).
        """
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        header_args: list[str] = []
        for k, v in _BROWSER_HEADERS.items():
            header_args += ["-H", f"{k}: {v}"]
        cmd = [
            "curl", "-sS", "--fail", "--compressed", "--location",
            "--max-time", "120", *header_args, "-o", str(tmp), url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"curl failed (rc={proc.returncode}) for {url}: "
                f"{proc.stderr.strip()[:200]}"
            )
        # Akamai serves 403s as an HTML error body; --fail catches HTTP
        # errors, but guard against a 200 soft-block too.
        with open(tmp, "rb") as fh:
            magic = fh.read(2)
        if magic != b"PK":
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"Downloaded payload from {url} is not an OOXML zip "
                f"(magic={magic!r}); likely an Akamai block page."
            )
        tmp.rename(out_path)
        return out_path

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Return the cached master workbook for `ym`.

        Edelweiss's master Excel is shared across all schemes, so
        `scheme_filename` is intentionally ignored. We re-download (idempotent)
        if discover wasn't called first.
        """
        master_path = self._cached_master_path(ym)
        if master_path.exists():
            return master_path
        return self._download_with_browser_headers(url, master_path)

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def parse_excel(
        self,
        excel_path: Path,
        scheme_name_printed: str,
        ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        """Locate the scheme's sheet in the consolidated workbook and parse it
        with the shared generic SEBI parser."""
        wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        try:
            sheet_code = self._find_sheet_for_scheme(wb, scheme_name_printed)
            if sheet_code is None:
                log.warning(
                    "holdings.edelweiss.scheme_not_in_index",
                    scheme=scheme_name_printed, ym=ym,
                )
                return
            recs = _parse_one_sheet(wb[sheet_code])
            if not recs:
                log.warning(
                    "holdings.edelweiss.empty_sheet",
                    scheme=scheme_name_printed, sheet=sheet_code, ym=ym,
                )
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
        wb: openpyxl.Workbook, scheme_name_printed: str
    ) -> str | None:
        """Reverse-lookup the Fund Id sheet code from the Index sheet by
        case-insensitive scheme-name match."""
        if "Index" not in wb.sheetnames:
            return None
        target = re.sub(r"\s+", " ", scheme_name_printed.strip().lower())
        idx_ws = wb["Index"]
        sheet_set = set(wb.sheetnames)
        for row in idx_ws.iter_rows(values_only=True):
            code = row[0] if len(row) > 0 else None
            raw_name = row[1] if len(row) > 1 else None
            if not code:
                continue
            code_s = str(code).strip()
            if code_s not in sheet_set:
                continue
            name = _scheme_name_from_index_cell(raw_name)
            if name and name.lower() == target:
                return code_s
        return None
