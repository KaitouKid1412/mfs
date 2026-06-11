"""Generic SEBI monthly-portfolio Excel parser + config-driven adapter.

SEBI mandates that every AMC publish a monthly portfolio statement in a
broadly standardized layout: a header row carrying columns that include
``ISIN``, a ``Name of the Instrument`` column, an industry/rating column,
Quantity, Market Value, and a ``% to NAV`` weight column (AMCs variously
print ``% to AUM`` / ``% of AUM`` / ``% to Net Assets`` / ``% of Net
Assets``).

The bespoke ``hdfc`` / ``sbi`` / ``nippon`` adapters hard-code column
offsets and the weight unit (percent vs fraction) because they predate this
module. Everything written from Phase 5 onward should reuse
``parse_sebi_excel`` instead: it DETECTS the header row, the ISIN / name /
weight column positions, and the weight unit at parse time, so one parser
handles the long tail of AMCs whose layouts differ only cosmetically.

A scheme's complete portfolio sums to ~100 (percent layout) or ~1.0
(fraction layout); we sum the raw weights and scale fractions up by 100.
This is more robust than a per-row magnitude heuristic (a fund whose top
holding is 1.2% would be misread as a fraction by a max-based rule).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import unquote, urljoin

import openpyxl

from mfs import paths
from mfs.ingest.holdings._base import HoldingsAdapter
from mfs.io.http import download_to, fetch_bytes
from mfs.schemas import ParsedHoldingRecord

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def is_isin(s: str) -> bool:
    """Strict ISIN check: 2 letters + 9 alphanumeric + 1 check digit."""
    return bool(_ISIN_RE.match(s))


def _norm(cell: object) -> str:
    """Lowercase, collapse-whitespace, strip-punctuation view of a cell for
    header matching. Returns '' for non-text / empty cells."""
    if not isinstance(cell, str):
        return ""
    s = cell.replace("\n", " ").strip().lower()
    s = re.sub(r"[^a-z0-9%]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Header-cell predicates. We match on normalized text so '% to NAV',
# '% to AUM', '% of AUM', '% to Net Assets' all resolve to the weight column.
def _is_isin_header(s: str) -> bool:
    return s == "isin" or s.startswith("isin ") or s.endswith(" isin")


def _is_weight_header(s: str) -> bool:
    if "%" not in s:
        return False
    return any(
        k in s
        for k in ("to nav", "of nav", "to aum", "of aum",
                  "to net asset", "of net asset", "to total", "of total")
    )


def _is_name_header(s: str) -> bool:
    if any(
        k in s
        for k in ("name of the instrument", "name of instrument",
                  "instrument issuer", "name of the issuer", "security name",
                  "company name", "name of instrument issuer")
    ) or s in ("instrument", "name"):
        return True
    # Broader catch for the "Company/Issuer/Instrument Name" header variant
    # (ICICI/Mirae ETFs): any non-weight header naming the instrument/issuer.
    # Excludes the weight column ('%') and the industry/rating column.
    if "%" in s or "industry" in s or "rating" in s:
        return False
    return "instrument" in s or "issuer" in s


def _is_industry_header(s: str) -> bool:
    return ("industry" in s or "rating" in s) and "%" not in s


def classify_section(label: str) -> str | None:
    """Map a section-banner string to an instrument_type, or None if the
    label is not a recognized section banner (caller keeps prior section).

    Shared SEBI-portfolio banner vocabulary across AMCs. Subtotal / total /
    grand-total footers return None so they don't reset the section.
    """
    if not label:
        return None
    s = label.strip().lower()
    if s in ("subtotal", "sub total", "total", "grand total", "sub-total"):
        return None
    if "equity" in s and "derivative" not in s:
        return "Equity"
    if any(k in s for k in (
        "debt", "money market", "triparty repo", "tri-party repo", "treps",
        "treasury", "government securities", "g-sec", "bonds", "debentures",
        "certificate of deposit", "commercial paper", "zero coupon",
        "preference shares", "non convertible", "securitised debt",
        "corporate debt", "fixed deposit",
    )):
        return "Debt"
    if "reit" in s or "invit" in s:
        return "REIT/InvIT"
    if any(k in s for k in (
        "net current asset", "net receivable", "net payable", "cash",
        "tbill", "t-bill", "cash margin", "cash & cash equivalent",
    )):
        return "Cash"
    return None


def _detect_header(rows: list[tuple], max_scan: int = 40) -> tuple[int, dict] | None:
    """Find the header row + column map. Returns (header_row_idx, colmap) or
    None. colmap keys: isin, name, weight, industry (industry optional)."""
    for i, row in enumerate(rows[:max_scan]):
        isin_col = name_col = weight_col = industry_col = None
        for j, cell in enumerate(row):
            s = _norm(cell)
            if not s:
                continue
            if isin_col is None and _is_isin_header(s):
                isin_col = j
            elif weight_col is None and _is_weight_header(s):
                weight_col = j
            elif name_col is None and _is_name_header(s):
                name_col = j
            elif industry_col is None and _is_industry_header(s):
                industry_col = j
        # A valid SEBI portfolio header has at minimum ISIN + weight + name.
        if isin_col is not None and weight_col is not None and name_col is not None:
            return i, {
                "isin": isin_col,
                "name": name_col,
                "weight": weight_col,
                "industry": industry_col,
            }
    return None


def _parse_one_sheet(ws) -> list[ParsedHoldingRecord] | None:
    """Parse a single worksheet in the generic SEBI layout. Returns the
    holding records (unscaled-then-scaled) or None if no header was found
    (i.e. this sheet is not a portfolio table)."""
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return None
    detected = _detect_header(rows)
    if detected is None:
        return None
    header_idx, cm = detected
    isin_c, name_c, weight_c = cm["isin"], cm["name"], cm["weight"]

    current_section = "Equity"
    raw: list[dict] = []
    for row in rows[header_idx + 1:]:
        ncols = max(isin_c, name_c, weight_c) + 1
        cells = list(row) + [None] * max(0, ncols - len(row))
        isin = cells[isin_c]
        name = cells[name_c]
        weight = cells[weight_c]
        isin_s = str(isin).strip() if isin is not None else ""
        name_s = name.strip() if isinstance(name, str) else ""

        if name_s.lower() == "grand total":
            break
        # Section banner: name present but ISIN col is not an ISIN.
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
        raw.append({
            "name": name_s.rstrip(" *"),
            "isin": isin_s,
            "weight": w,
            "section": current_section,
        })

    if not raw:
        return None

    # Weight-unit detection: a fraction-format portfolio's weights sum to
    # ~1.0 across ALL instrument types and never exceed ~1.0 in total; a
    # percent-format portfolio sums to ~100. So total ISIN-weight magnitude
    # > 1.5 ⇒ percent. This correctly classifies cash-heavy debt funds whose
    # few ISIN rows still sum > 1.5 in percent (a max-based or sum<5 rule
    # would misread those as fractions). The only residual ambiguity — a
    # fund with <1.5% total in ISIN-bearing instruments — is never an equity
    # fund, so it's irrelevant to the ranked universe.
    total = sum(abs(r["weight"]) for r in raw)
    scale = 100.0 if total <= 1.5 else 1.0

    out: list[ParsedHoldingRecord] = []
    seen: set[str] = set()
    for r in raw:
        if r["isin"] in seen:
            continue
        seen.add(r["isin"])
        out.append(ParsedHoldingRecord(
            scheme_name_printed="",  # filled by caller
            security_name=r["name"],
            weight_pct=r["weight"] * scale,
            isin=r["isin"],
            instrument_type=r["section"],
            source_amc="",  # filled by caller
        ))
    return out


def parse_sebi_excel(
    excel_path: Path,
    scheme_name_printed: str,
    source_amc: str,
    sheet_index: int | None = 0,
) -> Iterable[ParsedHoldingRecord]:
    """Parse a single-scheme SEBI monthly-portfolio Excel generically.

    Detects the header row, ISIN/name/weight columns, and the weight unit.
    Yields ISIN-bearing ``ParsedHoldingRecord`` rows with ``security_name``,
    ``weight_pct`` (always percent), ``isin``, and ``instrument_type``.

    ``sheet_index``: which sheet holds the portfolio. Default 0 (the common
    case). Pass ``None`` to scan ALL sheets and parse the first one that
    yields a recognizable SEBI header (useful when the equity table isn't on
    sheet 0). Multi-scheme consolidated workbooks need a bespoke adapter —
    this helper assumes one scheme per file.
    """
    wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
    try:
        if sheet_index is None:
            sheets = list(wb.worksheets)
        else:
            sheets = [wb.worksheets[sheet_index]] if sheet_index < len(wb.worksheets) else []
        for ws in sheets:
            recs = _parse_one_sheet(ws)
            if recs:
                for r in recs:
                    yield ParsedHoldingRecord(
                        scheme_name_printed=scheme_name_printed,
                        security_name=r.security_name,
                        weight_pct=r.weight_pct,
                        isin=r.isin,
                        instrument_type=r.instrument_type,
                        source_amc=source_amc,
                    )
                return  # first portfolio sheet wins
    finally:
        wb.close()


# ---------------------------------------------------------------------------
# Config-driven adapter base for the common case (one Excel per scheme,
# discoverable from a static disclosures HTML page).
# ---------------------------------------------------------------------------


def discover_xlsx_links(
    page_url: str,
    url_pattern: re.Pattern,
    scheme_from_filename: Callable[[str], str | None],
    *,
    base_url: str | None = None,
) -> dict[str, str]:
    """Scrape a disclosures HTML page for portfolio Excel links.

    Handles the dominant discovery shape: a (possibly SPA-rendered) HTML
    page whose markup embeds absolute or root-relative ``.xls``/``.xlsx``
    URLs. ``url_pattern`` must capture the full URL in group 1 (or group
    'url'); root-relative matches are absolutized against ``base_url`` (or
    the page origin). ``scheme_from_filename`` turns the URL's filename into
    the printed scheme name (return None to skip a file, e.g. aggregate
    "all-schemes" workbooks). First occurrence of a scheme name wins.
    """
    html = fetch_bytes(page_url).decode("utf-8", errors="replace")
    if base_url is None:
        m = re.match(r"(https?://[^/]+)", page_url)
        base_url = m.group(1) if m else ""
    has_named = "url" in url_pattern.groupindex
    out: dict[str, str] = {}
    for mt in url_pattern.finditer(html):
        url = mt.group("url") if has_named else mt.group(1)
        if url.startswith("/"):
            url = urljoin(base_url + "/", url.lstrip("/"))
        filename = unquote(url.rsplit("/", 1)[-1].split("?", 1)[0])
        scheme = scheme_from_filename(filename)
        if not scheme:
            continue
        out.setdefault(re.sub(r"\s+", " ", scheme).strip(), url)
    return out


class GenericHoldingsAdapter(HoldingsAdapter):
    """Base for AMCs that publish one SEBI-format Excel per scheme.

    Subclasses set ``amc_slug`` / ``source_label`` and implement
    ``discover_scheme_urls`` (often a one-liner via ``discover_xlsx_links``).
    ``parse_excel`` and ``fetch_excel`` are inherited — the shared
    ``parse_sebi_excel`` auto-detects columns + weight unit, and fetch uses
    the standard cached download. Override ``sheet_index = None`` when the
    portfolio table is not on the first sheet.
    """

    sheet_index: int | None = 0

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        out = paths.holdings_excel_raw(self.amc_slug, ym, scheme_filename)
        if out.exists():
            return out
        return download_to(url, out)

    def parse_excel(
        self, excel_path: Path, scheme_name_printed: str, ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        return parse_sebi_excel(
            excel_path, scheme_name_printed, self.amc_slug,
            sheet_index=self.sheet_index,
        )
