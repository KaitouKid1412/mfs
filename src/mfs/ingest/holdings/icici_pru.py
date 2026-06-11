"""ICICI Prudential monthly portfolio holdings adapter (Phase 5).

ICICI Prudential does NOT publish one Excel per scheme on a static page, and
it does NOT publish a single consolidated workbook either. Instead it ships
ONE password-free ZIP per month containing ~143 per-scheme ``.xlsx`` files
(one standard SEBI-portfolio Excel per scheme, named after the scheme).

Discovery is API-driven. The site (``www.icicipruamc.com``) is a
Create-React-App SPA whose "Other Scheme Disclosures > Monthly Portfolio
Disclosures" listing is served by a backing JSON API behind Azure Front
Door + an F5 gateway:

    GET  https://apimf.icicipruamc.com/nms/v1/downloads/categories?userType=Investor
    POST https://apimf.icicipruamc.com/nms/v1/downloads/files

Both require the request header ``env: api`` (the SPA's axios interceptor
sets ``headers.env = "api"`` on every call); without it the gateway returns
the SPA HTML shell with "Original Status Code: 404". The categories call
yields the subcategory id for "Monthly Portfolio Disclosures"
(``MONTHLY_PORTFOLIO_DISCSLO_DWND``); the files call (POST body
``{categoryId, userType, fileType, page, size}`` — note ``page``/``size``
are STRING-typed) returns one record per month, each with a relative
``url`` like::

    /downloads/Files/Monthly Portfolio Disclosures/2026/Apr/
        Monthly-Portfolio-Disclosure-April-2026.zip

We resolve the file by its ``applicableMonth`` epoch (the SEBI data-month
end) rather than the record's ``FINANCIAL_YEAR`` field — that field is
populated inconsistently on ICICI's side (the April-2026 record was tagged
``2024-2025``), so filtering on it drops valid months.

FILE HOST QUIRK: ``www.icicipruamc.com/downloads/*`` 307-redirects every
client (including real browsers) to ``archive.icicipruamc.com``, which does
not resolve in public DNS. The same bytes are served, without the redirect,
from the ``/blob`` prefix on the same host:

    https://www.icicipruamc.com/blob/downloads/Files/.../...zip

So we rewrite the API's relative ``url`` onto ``{HOST}/blob{url}``.

We download + extract the ZIP once per month, then treat each extracted
per-scheme Excel as the unit of work: ``discover_scheme_urls`` returns
``{scheme_name: local_extracted_path}``, and ``fetch_excel`` short-circuits
to that local path (the "url" is already a filesystem path).

Per-scheme Excel layout (validated against April-2026 Large Cap / Flexicap /
Midcap / Equity & Debt / Corporate Bond):
- Sheet 0 holds the portfolio (sheet name = scheme code, e.g. 'BLUECHIP');
  an optional 'Derivative' sheet follows and is ignored (Active Share is the
  cash-equity book; derivatives don't carry comparable ISIN rows).
- Row 0: AMC banner. Row 1: scheme name. Row 2: "Portfolio as on Apr 30,...".
- Row 3: header — col B (1)=Company/Issuer/Instrument Name, col C (2)=ISIN,
  col D (3)=Coupon, col E (4)=Industry/Rating, col F (5)=Quantity,
  col G (6)=Exposure/Market Value, col H (7)="% to Nav", col I (8)=Yield.
- Row 4+: section banners ("Equity & Equity Related", "Listed / Awaiting
  Listing on Stock Exchanges", "Debt Instruments", "Money Market", ...) carry
  text in the name column with an empty ISIN column; holding rows carry a
  real ISIN in col C.

We can't reuse ``parse_sebi_excel`` here: its header detector doesn't
recognize the "Company/Issuer/Instrument Name" name-column wording, so it
finds no header and yields nothing. We therefore use a bespoke,
column-aware parser (same pattern as the hdfc/sbi/nippon adapters).

CRITICAL UNIT NOTE: like Nippon, ICICI stores "% to Nav" as a FRACTION
(HDFC Bank in Large Cap shows 0.0885 for 8.85%); we multiply by 100 to the
canonical percent representation ``ParsedHoldingRecord.weight_pct`` expects.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import openpyxl

from mfs import paths
from mfs.ingest.holdings._generic import GenericHoldingsAdapter, classify_section
from mfs.ingest.holdings._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_API_BASE = "https://apimf.icicipruamc.com"
_FILE_HOST = "https://www.icicipruamc.com"
_CATEGORIES_EP = "/nms/v1/downloads/categories"
_FILES_EP = "/nms/v1/downloads/files"

# The SPA's axios interceptor stamps every request with this header; without
# it the gateway serves the SPA HTML shell instead of JSON.
_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "env": "api",
    "Origin": _FILE_HOST,
    "Referer": _FILE_HOST + "/",
}

# Subcategory code for the Monthly Portfolio Disclosures listing. We resolve
# its UUID dynamically from the categories endpoint rather than hardcoding,
# so a backend re-keying doesn't silently break discovery.
_SUBCATEGORY_CODE = "MONTHLY_PORTFOLIO_DISCSLO_DWND"

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")

# Layout constants for the per-scheme Excel (0-indexed columns, sheet 0).
_NAME_COL = 1
_ISIN_COL = 2
_WEIGHT_COL = 7


def _is_isin(s: str) -> bool:
    return bool(_ISIN_RE.match(s))


def _data_month_window(ym: str) -> tuple[datetime, datetime]:
    """[start, next_month_start) in UTC for matching the file's
    ``applicableMonth`` epoch to the requested data month."""
    y, m = map(int, ym.split("-"))
    start = datetime(y, m, 1, tzinfo=UTC)
    nxt = (
        datetime(y + 1, 1, 1, tzinfo=UTC)
        if m == 12
        else datetime(y, m + 1, 1, tzinfo=UTC)
    )
    return start, nxt


def _zip_cache_path(ym: str) -> Path:
    """Local cache path for the month's consolidated ZIP."""
    return paths.holdings_excel_raw(
        "icici_pru", ym, f"Monthly-Portfolio-Disclosure-{ym}.zip"
    )


@register_adapter
class IciciPruHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "icici_pru"
    source_label = "ICICI Prudential Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Find the month's portfolio ZIP via the downloads API, download +
        extract it once, and return {scheme_name: local_extracted_path}.

        Every value is a filesystem path, not an HTTP URL — ``fetch_excel``
        short-circuits to it.
        """
        zip_url = self._find_zip_url(ym)
        if zip_url is None:
            log.warning("holdings.icici_pru.no_zip_for_ym", ym=ym)
            return {}

        extract_dir = self._ensure_extracted(zip_url, ym)
        out: dict[str, str] = {}
        for xlsx in sorted(extract_dir.glob("*.xlsx")):
            scheme = re.sub(r"\s+", " ", xlsx.stem).strip()
            if scheme:
                out.setdefault(scheme, str(xlsx))
        log.info(
            "holdings.icici_pru.discover",
            ym=ym, n_schemes=len(out), zip_url=zip_url,
        )
        return out

    def _find_zip_url(self, ym: str) -> str | None:
        """Hit the downloads API and return the absolute ZIP URL whose
        ``applicableMonth`` falls in data month ``ym``."""
        with httpx.Client(
            timeout=60.0, headers=_API_HEADERS, follow_redirects=True
        ) as c:
            cat_id = self._resolve_subcategory_id(c)
            if cat_id is None:
                log.warning("holdings.icici_pru.no_subcategory")
                return None
            body = json.dumps({
                "categoryId": cat_id,
                "userType": "Investor",
                "fileType": "All",
                "page": "1",
                "size": "60",
            })
            r = c.post(_API_BASE + _FILES_EP, content=body)
            r.raise_for_status()
            files = (
                (r.json() or {}).get("success", {}) or {}
            ).get("data", {}).get("files", []) or []

        start, nxt = _data_month_window(ym)
        for f in files:
            applicable = f.get("applicableMonth")
            url = f.get("url")
            if not applicable or not url:
                continue
            dt = datetime.fromtimestamp(applicable / 1000, tz=UTC)
            if start <= dt < nxt and url.lower().endswith(".zip"):
                return self._absolutize(url)
        return None

    @staticmethod
    def _resolve_subcategory_id(c: httpx.Client) -> str | None:
        """Walk the categories tree for the Monthly Portfolio Disclosures
        subcategory id."""
        r = c.get(_API_BASE + _CATEGORIES_EP, params={"userType": "Investor"})
        r.raise_for_status()
        cats = ((r.json() or {}).get("success", {}) or {}).get("data", []) or []
        for cat in cats:
            for sub in cat.get("subCategory", []) or []:
                code = (sub.get("title", {}) or {}).get("code")
                if code == _SUBCATEGORY_CODE:
                    return sub.get("id")
        return None

    @staticmethod
    def _absolutize(rel_url: str) -> str:
        """Rewrite an API-relative download path onto the /blob file host.

        ``www.icicipruamc.com/downloads/*`` 307-redirects to a host that
        doesn't resolve publicly; the same bytes serve from ``/blob`` on the
        same origin without the redirect.
        """
        if rel_url.startswith("http"):
            return rel_url
        path = rel_url if rel_url.startswith("/") else "/" + rel_url
        return f"{_FILE_HOST}/blob{path}"

    def _ensure_extracted(self, zip_url: str, ym: str) -> Path:
        """Download (cached) + extract the month's ZIP; return the extract dir."""
        zip_path = _zip_cache_path(ym)
        extract_dir = zip_path.parent / "extracted"
        # If already extracted with at least one xlsx, reuse.
        if extract_dir.exists() and any(extract_dir.glob("*.xlsx")):
            return extract_dir
        if not zip_path.exists():
            self._download_zip(zip_url, zip_path)
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                if not member.lower().endswith(".xlsx"):
                    continue
                # Flatten any internal directory structure to basename.
                name = Path(member).name
                if not name:
                    continue
                with zf.open(member) as src:
                    (extract_dir / name).write_bytes(src.read())
        return extract_dir

    @staticmethod
    def _download_zip(url: str, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(
            timeout=180.0,
            headers={"User-Agent": _API_HEADERS["User-Agent"],
                     "Referer": _FILE_HOST + "/"},
            follow_redirects=True,
        ) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.content
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(out_path)
        return out_path

    # ------------------------------------------------------------------
    # Fetch (local short-circuit)
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """The "url" produced by discovery is already a local extracted path."""
        p = Path(url)
        if p.exists():
            return p
        # Defensive: re-extract if the cache was cleared between discover and
        # fetch (e.g. a test invoked fetch_excel directly).
        raise FileNotFoundError(
            f"icici_pru extracted Excel not found: {url}. Run "
            f"discover_scheme_urls({ym!r}) first to populate the cache."
        )

    # ------------------------------------------------------------------
    # Parse (bespoke column-aware; generic detector misses the name header)
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
            current_section = "Equity"
            seen: set[str] = set()
            ncols = _WEIGHT_COL + 1
            for row in ws.iter_rows(values_only=True):
                cells = list(row) + [None] * max(0, ncols - len(row))
                name = cells[_NAME_COL]
                isin = cells[_ISIN_COL]
                weight = cells[_WEIGHT_COL]

                name_s = name.strip() if isinstance(name, str) else ""
                isin_s = str(isin).strip() if isin is not None else ""

                if name_s.lower() == "grand total":
                    break
                # Section banner: name text present, ISIN col not an ISIN.
                if name_s and not _is_isin(isin_s):
                    section = classify_section(name_s)
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
                    security_name=name_s.rstrip(" *"),
                    weight_pct=w * 100.0,  # ICICI stores % to Nav as a fraction
                    isin=isin_s,
                    instrument_type=current_section,
                    source_amc=self.amc_slug,
                )
        finally:
            wb.close()
