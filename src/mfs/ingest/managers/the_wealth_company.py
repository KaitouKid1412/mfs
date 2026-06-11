"""The Wealth Company Mutual Fund — factsheet adapter (Phase 3 PTR coverage).

Calibrated against the April 2026 combined factsheet at
``data/raw/factsheets/the_wealth_company/2026-04.pdf`` (~3.8 MB, 30 pages).

The Wealth Company is India's first woman-led AMC (SEBI final approval July
2025; first NFOs allotted 14 Oct 2025). Its combined monthly factsheet carries
one page per scheme (pages 6-14 in the 2026-04 PDF: Flexi Cap, Ethical, Small
Cap, Arbitrage, Multi Asset Allocation, Balanced Advantage, Liquid, Gold ETF,
Gold ETF FOF) plus front-matter (cover, index, "How to Read the Factsheet"
glossary, investment philosophy) and back-matter (performance, fund-manager
list, riskometers, snapshot grid, market review, marketing).

``amc_slug`` ("the_wealth_company") matches ``scheme_master.amc_code``
directly (scheme_master carries DIRECT+GROWTH rows under that amc_code), so no
alias entry in ``_scheme_match.py`` is required.

PTR availability — IMPORTANT
----------------------------
**This AMC does NOT print a per-scheme Portfolio Turnover Ratio anywhere in
the factsheet.** A word-level scan of every page of the 2026-04 PDF found the
token "Turnover" only on page 3, inside the "How to Read the Factsheet"
glossary ("Portfolio Turnover Ratio: ... the percentage of a fund's holdings
that have changed ..."). No scheme page and no "Snapshot of Funds" grid row
carries a turnover value.

This is expected for an AMC whose funds are all <8 months old (allotment
14-Oct-2025 onward) — PTR is conventionally a trailing-12-month metric and is
omitted until a fund completes a year. The per-scheme pages instead disclose:
AUM (Monthly Avg + Month-end), Total Expense Ratio (Regular/Direct), NAV,
the full portfolio holdings table, and Top-5 Stock/Sector holdings; debt
funds additionally print YTM, Macaulay duration and Modified duration.

``parse_ptr`` below is written to extract PTR the moment this AMC starts
printing it (text regex over the common label variants, plus a word-position
fallback for column-bleed), but against the 2026-04 PDF it correctly yields
**zero** records rather than fabricating values — half data is worse than no
data. When a future factsheet adds the metric, re-run and the existing parser
should pick it up; verify the printed label and unit against the regex
comment then.

URL discovery
-------------
Factsheets live on the AMC's CMS at
``https://www.wealthcompanyamc.in/literature-forms/scheme-documents/factsheets/``
The PDFs are served from ``/uploads/<name>_<cms-hash>.pdf`` where the trailing
hash (e.g. ``_c6d93a6643``) is an unpredictable Strapi upload id and the stem
itself is inconsistent month-over-month:

    The_Wealth_Company_MF_Factsheet_Apr_2026_<hash>.pdf     (Apr 2026)
    The_Wealth_Company_MF_Factsheet_Mar_2026_<hash>.pdf     (Mar 2026)
    Factsheet_as_on_28_02_2026_<hash>.pdf                   (Feb 2026)
    The_Wealth_Company_MF_Factsheet_January2026_<hash>.pdf  (Jan 2026)
    Factsheet_as_on_31_12_2025_<hash>.pdf                   (Dec 2025)

Because neither the hash nor the stem is deterministic, ``build_url(ym)``
returns the stable factsheets listing page (the public route a human would
follow) and ``fetch()`` scrapes it for the one factsheet link whose filename
encodes the target data month/year (handling both the ``Apr_2026`` /
``April2026`` month-name style and the ``as_on_DD_MM_YYYY`` data-date style).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pdfplumber

from mfs.errors import IngestError
from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_FACTSHEET_PAGE = (
    "https://www.wealthcompanyamc.in/literature-forms/scheme-documents/factsheets/"
)

_MONTH_FULL = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_ABBR = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


def _factsheet_matches_month(url: str, year: int, month: int) -> bool:
    """True if the factsheet filename encodes data month=(year, month).

    Two filename styles are seen on the AMC's CMS, both supported here:

      * month-name style — ``..._Apr_2026_<hash>.pdf`` /
        ``..._April2026_<hash>.pdf``: a full or 3-letter month token AND a
        4-digit year token, each with non-letter / non-digit boundaries so
        'mar' doesn't match inside 'march' and a year fragment isn't matched
        mid-number.
      * data-date style — ``Factsheet_as_on_30_04_2026_<hash>.pdf``: a
        ``DD_MM_YYYY`` group whose month/year components match.

    Filenames that encode neither (e.g. the Oct/Nov 2025 launch PDFs named
    ``..._Fact_Sheet_V3_*``) cannot be resolved by month and never match —
    fail-fast rather than guess.
    """
    fn = url.rsplit("/", 1)[-1].lower()
    full = _MONTH_FULL[month - 1]
    abbr = _MONTH_ABBR[month - 1]
    y4 = str(year)

    # Style 1: month-name + 4-digit year tokens.
    has_month = re.search(rf"(?<![a-z])(?:{full}|{abbr})(?![a-z])", fn) is not None
    has_year = re.search(rf"(?<!\d){y4}(?!\d)", fn) is not None
    if has_month and has_year:
        return True

    # Style 2: "as on DD_MM_YYYY" data-date token.
    for dm in re.finditer(r"(?<!\d)(\d{2})[_\-.](\d{2})[_\-.](\d{4})(?!\d)", fn):
        _dd, mm, yyyy = dm.group(1), int(dm.group(2)), int(dm.group(3))
        if mm == month and yyyy == year:
            return True
    return False


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from the first non-empty line, or None.

    Each per-scheme page's first non-empty line is the full scheme name
    (e.g. ``The Wealth Company Flexi Cap Fund``). Non-scheme pages (cover
    ``Factsheet as on``, ``Index``, ``How to Read the Factsheet``,
    ``Scheme Performance``, ``Riskometer``, ``Snapshot of Funds``,
    ``Economic Overview``, ``Market Review``, ``MUTUAL FUND`` marketing) do
    not start with the AMC prefix and are dropped.

    NOTE: this is wired for completeness so a future PTR-bearing factsheet can
    be parsed without further name-detection work. Against the 2026-04 PDF no
    scheme page carries a turnover value, so ``parse_ptr`` yields nothing
    regardless.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.lower().startswith("the wealth company "):
        return None
    first = re.sub(r"\s+", " ", first).strip()
    # Drop banner-only first lines that aren't a fund (e.g. a page whose first
    # line is the company name without a "Fund" terminator).
    if "fund" not in first.lower() and "etf" not in first.lower():
        return None
    return first or None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# The Wealth Company does NOT currently print PTR (see module docstring); this
# regex is staged for the first factsheet that does. Label variants observed
# across Indian AMCs: "Portfolio Turnover Ratio", "Portfolio Turnover",
# "Turnover Ratio", "Equity Turnover", "PTR". The captured number's UNIT must
# be verified when the metric first appears — divide by 100 ONLY if printed as
# a percent ("127%" / "127.00"); pass through if printed as a fraction
# ("1.27"). The parser detects a trailing "%" and converts accordingly.
_PTR_TEXT_RE = re.compile(
    r"(?:Portfolio\s+Turnover(?:\s+Ratio)?|Equity\s+Turnover|Turnover\s+Ratio|PTR)"
    r"\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(%?)",
    re.IGNORECASE,
)


def _find_ptr_via_words(page) -> tuple[float, bool] | None:
    """Word-position fallback for PTR — returns (value, is_percent) or None.

    Locate a ``Turnover`` token in the left column (x0 < 280) and take the
    first numeric token to its right on the same tight y-band (±2pt). The
    boolean reports whether that token (or the next one) carried a ``%`` so
    the caller can normalise to a fraction. Staged for future PTR-bearing
    factsheets; bails immediately on the current PDF (no ``Turnover`` token
    exists outside the page-3 glossary, which has no scheme name).
    """
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None
    for w in words:
        if not w["text"].lower().startswith("turnover"):
            continue
        if w["x0"] >= 280:
            continue
        ybot = w["bottom"]
        nums = [
            v for v in words
            if abs(v["bottom"] - ybot) <= 2
            and v["x0"] > w["x1"]
            and re.fullmatch(r"\d+(?:\.\d+)?%?", v["text"].rstrip(",;."))
        ]
        nums.sort(key=lambda v: v["x0"])
        for v in nums:
            tok = v["text"].rstrip(",;.")
            is_pct = tok.endswith("%")
            try:
                val = float(tok.rstrip("%"))
            except ValueError:
                continue
            if val > 0:
                return val, is_pct
        return None
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@register_adapter
class TheWealthCompanyAdapter(ManagerAdapter):
    """The Wealth Company Mutual Fund factsheet adapter."""

    amc_slug = "the_wealth_company"
    source_label = "The Wealth Company Mutual Fund"

    # -------------------------------------------------------------------
    # URL / fetch
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the stable factsheets listing-page URL for data month ym.

        The per-month PDF lives under ``/uploads/`` with an unpredictable CMS
        hash and an inconsistent stem (see module docstring), so the canonical
        entry point is the public listing page; ``fetch()`` resolves the
        per-month PDF from it. ``ym`` is accepted for interface symmetry with
        deterministic adapters.
        """
        return _FACTSHEET_PAGE

    def fetch(self, ym: str) -> Path:
        """Download the combined factsheet for data month ym='YYYY-MM'.

        Scrapes the factsheets listing page and selects the single factsheet
        link whose filename encodes the target data month/year (month-name or
        ``as_on_DD_MM_YYYY`` style). Re-uses an existing cached PDF
        unconditionally — delete the file to force a re-fetch.
        """
        from mfs import paths
        from mfs.io.http import download_to, fetch_bytes

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out

        year, month = map(int, ym.split("-"))
        page_url = self.build_url(ym)
        try:
            html = fetch_bytes(page_url).decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            raise IngestError(
                f"the_wealth_company: failed to fetch factsheets page "
                f"{page_url}: {e}"
            ) from e

        urls = re.findall(r"https?://[^\"'\s<>\\]+?\.pdf", html, re.IGNORECASE)
        candidates: list[str] = []
        for u in dict.fromkeys(urls):  # dedupe, preserve order
            fn = u.rsplit("/", 1)[-1].lower()
            if "factsheet" not in fn and "fact_sheet" not in fn:
                continue
            if "kim" in fn or "sid" in fn or "addendum" in fn:
                continue
            if _factsheet_matches_month(u, year, month):
                candidates.append(u)

        if not candidates:
            raise IngestError(
                f"the_wealth_company: no combined factsheet PDF for data month "
                f"{ym} found on {page_url}. The listing-page layout or filename "
                "convention may have changed — please re-discover."
            )
        if len(candidates) > 1:
            log.warning(
                "the_wealth_company.fetch.ambiguous", ym=ym, candidates=candidates,
            )
        return download_to(candidates[0], out)

    # -------------------------------------------------------------------
    # PTR
    # -------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        """Yield one ParsedPtrRecord per scheme that prints a PTR.

        Against the 2026-04 PDF this yields **nothing** — the AMC does not
        print a per-scheme turnover value (see module docstring). The two-stage
        extraction below is staged for the first factsheet that does:
          1. text regex over the common label variants;
          2. word-position fallback for column-bleed.
        Both normalise to a fraction (divide by 100 only when the source
        printed a percent). Fail-fast drops NaN / non-positive values.
        """
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                if "turnover" not in text.lower():
                    continue
                # Strategy 1: text regex (label + value, optional %).
                ptr_value: float | None = None
                is_pct = False
                m = _PTR_TEXT_RE.search(text)
                if m:
                    try:
                        ptr_value = float(m.group(1))
                        is_pct = m.group(2) == "%"
                    except ValueError:
                        ptr_value = None
                # Strategy 2: word-position fallback for column-bleed.
                if ptr_value is None:
                    hit = _find_ptr_via_words(page)
                    if hit is not None:
                        ptr_value, is_pct = hit
                if ptr_value is None:
                    continue
                # Fail-fast: drop NaN / non-positive.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                # Normalise to a fraction: divide by 100 iff printed as percent.
                if is_pct:
                    ptr_value = ptr_value / 100.0
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )

    # Holdings: left to the base-class default (Excel-based Phase 3.C path is
    # the canonical ISIN-tagged holdings source). Not overridden here.
