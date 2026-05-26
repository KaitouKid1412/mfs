"""UTI Mutual Fund — factsheet adapter (Phase 3.B).

Calibrated against the April 2026 "UTI Fund Watch (Active)" combined
factsheet PDF (~7.96 MB, 110 pages) hosted at:
  https://d3ce1o48hc5oli.cloudfront.net/s3fs-public/2026-05/
    uti_fund_watch_active_april_2026.pdf

URL pattern was discovered by reading the SPA's CMS JSON endpoint at
  https://www.utimf.com/api/get-fact-sheet?year=YYYY&month=MonthName
which exposes the per-month "Fund Watch (Active)" CloudFront URL. The
filename pattern is stable; the publish-month folder is data_month + 1.

Layout findings driving the parser:
- Each scheme page first non-empty line either starts with "UTI " and ends
  with something like "UTI <NAME> FUND Category" (the trailing "Category"
  is a UI tag word fused onto the title), or — for sectoral/thematic
  schemes — the first line is just "Category" with the actual scheme
  title on line 2. We accept the first line in the first 4 lines that
  matches `^UTI <...> (FUND|FOF)\b`.
- AUM is printed as `Closing AUM :` ` <number> Crore` (Indian rupee
  symbol rendered as a backtick by pdfplumber). Already in INR Crore so
  no unit conversion needed. We prefer Closing AUM over Monthly Average.
- Portfolio Turnover is printed as `Portfolio Turnover <fraction>` —
  already a fraction (UTI Large Cap Fund = 0.41, i.e. 41%). On equity
  scheme pages the text-flow layout breaks "Portfolio Turnover" with
  column-bleed (becomes `P R o ati rtf o o ( l A io n T n u u r a n l)
  over 0.41`) so we use a two-step extraction: first try the clean text
  regex (works on hybrid / debt / page-2 layouts), then fall back to a
  word-position search (handles the equity page-1 column-bleed).
- Some pages print `Closing AUM: ` 0.0 Crore` — this is the segregated-
  portfolio split-out page for UTI Conservative Hybrid Fund. We drop any
  AUM <= 0 to avoid polluting Stage 2 with phantom zero-AUM rows.
- Cover / TOC / market-review / index / IDCW-history pages either don't
  start with "UTI <...> Fund" or end up with no PTR/AUM marker and are
  silently skipped (the parser yields nothing for those pages).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pdfplumber

from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_MONTH_FULL = (
    "January|February|March|April|May|June|"
    "July|August|September|October|November|December"
)
_MONTH_NAMES_LIST = _MONTH_FULL.split("|")


def _publish_ym(data_ym: str) -> str:
    """data month YYYY-MM -> publish month YYYY-MM (data + 1, with year rollover)."""
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{m + 1:02d}"


# Scheme title detector. Matches lines like:
#   "UTI LARGE CAP FUND Category"
#   "UTI BANKING & FINANCIAL SERVICES FUND"
#   "UTI INCOME PLUS ARBITRAGE ACTIVE FUND OF FUND"
#   "UTI - Flexi Cap Fund"
# But NOT "UTI MUTUAL FUND IN MEDIA" (cover) or list-style summary pages.
_SCHEME_LINE_RE = re.compile(
    r"^(UTI\b[\w\s&\-’\'.]*?(?:FUND(?:\s+OF\s+FUND)?|FOF))(?:\b|@|$)",
    re.IGNORECASE,
)
# Lines that masquerade as scheme titles but aren't (cover ads, TOC, etc).
_NOT_SCHEME_RE = re.compile(
    r"\bUTI MUTUAL FUND\b|\bIN MEDIA\b|^UTI\s+MUTUAL\s+FUND\s*$",
    re.IGNORECASE,
)

# PTR (clean text path) — works on hybrid / debt / page-2 layouts where
# pdfplumber's extract_text does not column-bleed.
_PTR_TEXT_RE = re.compile(
    r"Portfolio\s+Turnover[^0-9\n]{0,30}(\d+\.\d+)",
    re.IGNORECASE,
)
def _scheme_name_from_page(text: str) -> str | None:
    """Inspect the first 4 non-empty lines for an `^UTI ...Fund` title.

    Returns the matched scheme title (with trailing "Category"/whitespace
    trimmed) or None for non-scheme pages.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:4]
    for ln in lines:
        if _NOT_SCHEME_RE.search(ln):
            continue
        m = _SCHEME_LINE_RE.match(ln)
        if not m:
            continue
        name = m.group(1).strip()
        # Drop spurious 'Category' or trailing punctuation if it leaked in.
        name = re.sub(r"\s+Category$", "", name, flags=re.IGNORECASE).strip()
        # Drop a trailing '@' (UTI marks one scheme — UTI Banking & PSU
        # Fund@ — with a footnote symbol).
        name = name.rstrip("@").strip()
        if not name.upper().startswith("UTI"):
            continue
        return name
    return None


def _find_ptr_via_words(page) -> float | None:
    """Locate 'Portfolio Turnover <num>' via word positions.

    Used when ``extract_text`` mangles the line via column-bleed (common on
    equity scheme pages). Finds the words 'Portfolio' and 'Turnover'
    adjacent in the same y-band, then takes the next numeric word to the
    right as the value.
    """
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None
    for w in words:
        if w["text"] != "Portfolio":
            continue
        for v in words:
            if v["text"] != "Turnover":
                continue
            if abs(v["bottom"] - w["bottom"]) > 3:
                continue
            if not (w["x1"] < v["x0"] < w["x1"] + 30):
                continue
            for n in words:
                if abs(n["bottom"] - v["bottom"]) > 3:
                    continue
                if n["x0"] <= v["x1"]:
                    continue
                if re.fullmatch(r"\d+\.\d{1,3}", n["text"]):
                    try:
                        return float(n["text"])
                    except ValueError:
                        return None
            break
    return None


@register_adapter
class UtiAdapter(ManagerAdapter):
    """UTI Mutual Fund factsheet adapter.

    ``amc_slug`` matches ``scheme_master.amc_code`` directly, so no alias
    entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "uti"
    source_label = "UTI Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical UTI Fund Watch (Active) PDF URL for data
        month ym='YYYY-MM'.

        UTI publishes the April 2026 issue in May 2026; the CloudFront
        folder reflects the publish month, while the filename embeds the
        data month.
        """
        y, m = map(int, ym.split("-"))
        publish = _publish_ym(ym)
        month_lower = _MONTH_NAMES_LIST[m - 1].lower()
        return (
            "https://d3ce1o48hc5oli.cloudfront.net/s3fs-public/"
            f"{publish}/uti_fund_watch_active_{month_lower}_{y}.pdf"
        )

    # -----------------------------------------------------------------
    # PTR / AUM
    # -----------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                ptr_value: float | None = None
                m = _PTR_TEXT_RE.search(text)
                if m:
                    try:
                        ptr_value = float(m.group(1))
                    except ValueError:
                        ptr_value = None
                if ptr_value is None:
                    ptr_value = _find_ptr_via_words(page)
                if ptr_value is None:
                    continue
                # Drop NaN / non-positive (UTI prints a real fraction or
                # nothing — there's no zero PTR scenario in practice).
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )

    # -----------------------------------------------------------------
    # Holdings (deferred to Phase 3.C — factsheet PDF lines mix sector
    # banners, "Others", "Net Current Assets" rows, and lack ISINs. We
    # leave this empty for the factsheet path and rely on the parallel
    # Excel-based ISIN-tagged ingest for portfolio coverage.)
    # -----------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
