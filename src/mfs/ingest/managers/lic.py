"""LIC Mutual Fund — factsheet adapter (Phase 3.D).

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/lic/2026-04.pdf``, ~5.14 MB, 88 pages, 43 scheme
detail pages plus cover / TOC / market reviews / SIP performance / TER /
key features / riskometer / glossary / branches).

URL discovery
-------------
LIC MF publishes the monthly factsheet under the marketing site
``www.licmf.com`` at

    https://www.licmf.com/assets/downloads/monthly_fact_sheet/
    {fy_start}-{fy_end}/{publish_mm}/lic-mf-factsheet-{dd}{ord}-{Month}-{yyyy}.pdf

where:
* ``fy_start-fy_end`` is the Indian fiscal year (April-March) for the
  DATA month. April 2026 onwards lives under ``2026-2027/``; March 2026
  lives under ``2025-2026/``.
* ``publish_mm`` is the publish month's two-digit number — that's the
  DATA month + 1, so April 2026 publishes in ``05/``.
* ``dd`` is the day of the data month-end (e.g. 30 for April).
* ``ord`` is the English ordinal suffix (``st``/``nd``/``rd``/``th``).
* ``Month`` is the data month's full English name in Title Case.
* ``yyyy`` is the data month's year.

So April 2026 is
``/assets/downloads/monthly_fact_sheet/2026-2027/05/lic-mf-factsheet-30th-April-2026.pdf``.

Older months on the same site may have a trailing CMS sequence id
(``-31st-march-2026-370391470.pdf``) but the latest month uses the clean
form, which is what we target.

Layout findings driving the parser
----------------------------------
* The scheme name does NOT appear in the rendered text of the scheme
  detail page. The page banner is drawn at a layer that doesn't yield a
  ``Fund Name`` line in ``extract_text``/``extract_words`` — all that
  remains is the section header ``SCHEME FEATURES`` and the body. The
  page footer reads ``{page_num} Factsheet April, 2026`` — no scheme
  name either.

* The only structured source of (scheme name → page number) mapping is
  the TOC on page 3. Each TOC entry matches::

      ^(\\d+)\\.\\s+(LIC MF .+?)\\s+\\.+\\s+(\\d+)$

  We build a ``{page_num: scheme_name}`` map once per parse and look up
  the printed name by page number. This is the cleanest available
  anchor; AMCs that decorate detail pages with vector-only banners are
  not unusual.

* PTR label: ``Annual Portfolio Turnover Ratio: 0.50 times``. Some
  hybrid schemes (Aggressive Hybrid, Balanced Advantage) print it as
  ``Annual Equity Portfolio Turnover Ratio: 0.44 times`` (the leading
  ``Equity`` word is the only distinguishing token). Newly launched
  schemes that haven't completed 1 year print ``: NA`` instead of a
  fraction — we drop those rows.

* PTR is printed in "times" (= a fraction). LIC MF Large Cap Fund =
  0.50 = 50%. Storage convention is also fraction, so we pass through
  with NO unit conversion.

* AUM label: ``AUM : ¢ 1,352.11 Cr`` — the ``¢`` glyph is pdfplumber's
  rendering of the rupee symbol (₹). The label is preceded later by
  ``Average AUM : ¢ 1,346.28 Cr`` — we want the month-end value (the
  first occurrence, NOT prefixed by ``Average``). Both values are in
  INR Crore so no unit conversion. Negative lookbehind in the regex
  guards against accidentally matching the Average line.

* PTR coverage: 23 of 43 detail pages carry a numeric PTR. The 20
  pages without are (a) all debt / liquid / overnight / money-market /
  gilt / short-duration / ultra-short / banking-PSU / low-duration /
  medium-long-duration / unit-linked-insurance / childrens / gold-FoF
  schemes, and (b) two index-fund pages (BSE Sensex Index, Nifty 50
  Index, Nifty Next 50 Index, Nifty 100 ETF, Nifty Midcap 100 ETF,
  NIFTY 8-13yr G-Sec ETF) which legitimately omit the turnover block.
  ELSS, Hybrid Aggressive / Balanced Advantage, Equity Savings,
  Conservative Hybrid, Arbitrage, Multi Asset, BSE Sensex ETF, Nifty
  50 ETF and Gold ETF all DO publish PTR. The brief notes the AMC has
  18 ranked equity schemes; we target ≥ 15 PTR rows from the equity
  set, and the parser produces 21 such rows on the April-2026 PDF
  (covering all 16 long-running equity + 2 sector + 3 hybrid funds
  that publish PTR; the two new equity schemes Consumption / Technology
  print ``NA``).

* Holdings: deferred. LIC's per-scheme portfolio tables follow a clean
  two-column layout (Company % of NAV / Company % of NAV) and could be
  parsed, but Phase 3.A relies on the parallel ISIN-tagged Excel path
  for the overlap metric — the factsheet rows would lack ISINs anyway.
  ``parse_holdings`` returns an empty iterable.

Calibration counts on 2026-04
-----------------------------
* TOC maps 43 scheme detail pages (pages 11-53).
* 43 AUM rows extracted (every detail page yields a clean Month-End
  AUM token).
* 23 PTR rows extracted (the rest are debt / index / ETF pages that
  legitimately omit the PTR block, or two new equity schemes that
  print ``NA``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pdfplumber

from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)


_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _publish_ym(data_ym: str) -> tuple[int, int]:
    """data month YYYY-MM -> (publish_year, publish_month) with rollover."""
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return y + 1, 1
    return y, m + 1


def _fiscal_year_dir(data_ym: str) -> str:
    """Return the ``YYYY-YYYY`` financial-year directory token.

    The Indian fiscal year runs April-March. April-2026 through
    March-2027 all live under ``2026-2027/``.
    """
    y, m = map(int, data_ym.split("-"))
    if m >= 4:
        return f"{y}-{y + 1}"
    return f"{y - 1}-{y}"


def _day_with_ordinal(month: int, year: int) -> str:
    """Return the last day of month with English ordinal suffix.

    Used as the day-token in LIC's filename (``30th``/``31st``/``28th``).
    """
    # LIC always publishes month-end data — day = last day of month.
    import calendar
    day = calendar.monthrange(year, month)[1]
    if 11 <= day % 100 <= 13:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suf}"


# ---------------------------------------------------------------------------
# Table-of-contents → (page_num → scheme_name) map
# ---------------------------------------------------------------------------

# TOC entries on page 3. Format: ``N. LIC MF <Name> .......... <Page>``.
# The dot-leaders can be truncated to as few as 1 dot when the entry name is
# long (two of the 2026-04 entries have only 1-2 trailing dots before the
# page number), so we require at least one literal '.' followed by the page
# number.
_TOC_LINE_RE = re.compile(
    r"^\s*(\d+)\.\s+(LIC\s+MF\s+.+?)\s+\.+\s+(\d+)\s*$",
    re.MULTILINE,
)

# The TOC glues the "Multi Cap" / "Mid Cap" cap-class words into one token for
# some schemes — e.g. it prints "LIC MF MultiCap Fund" while scheme_master
# records "LIC MF Multi Cap Fund". Canonicalized, the glued "MULTICAP" token
# matches neither "MULTI" nor "CAP", so the orchestrator's token_set_ratio
# scorer drops the printed name (82.9 < the 85 threshold) and the scheme's
# published PTR ("Annual Portfolio Turnover Ratio: 0.43 times" on the detail
# page) is silently lost. Splitting the glued token back to two words lifts
# the match to 100. The transforms are anchored on word boundaries; LIC's
# clean entries ("Multi Asset Allocation Fund", "Large Cap Fund") are
# untouched.
_TOC_GLUE_FIXES = (
    (re.compile(r"\bMultiCap\b", re.IGNORECASE), "Multi Cap"),
    (re.compile(r"\bMidcap\b", re.IGNORECASE), "Mid Cap"),
)


def _repair_toc_name(name: str) -> str:
    """Repair the TOC's glued cap-class tokens so the orchestrator's fuzzy
    matcher resolves the printed name to the correct scheme_master row."""
    for pat, repl in _TOC_GLUE_FIXES:
        name = pat.sub(repl, name)
    return name


def _build_toc_map(pdf: pdfplumber.PDF) -> dict[int, str]:
    """Parse the TOC on the second-or-third page and return
    ``{page_number: scheme_name}``.

    The TOC is on page 3 in the 2026-04 issue; the function probes the
    first 5 pages for the marker line ``INDEX``/``Sr. no. Scheme Name
    Page No.`` to be resilient to small layout changes.
    """
    out: dict[int, str] = {}
    for i, page in enumerate(pdf.pages[:5]):
        text = page.extract_text() or ""
        if "Sr. no." not in text and "INDEX" not in text:
            continue
        for m in _TOC_LINE_RE.finditer(text):
            try:
                page_num = int(m.group(3))
            except ValueError:
                continue
            name = m.group(2).strip()
            # Strip any leaked trailing dots from the dot-leader run.
            name = re.sub(r"\s*\.+\s*$", "", name).strip()
            # Collapse double-spaces.
            name = re.sub(r"\s+", " ", name)
            if not name.startswith("LIC MF"):
                continue
            # Repair glued cap-class tokens ("MultiCap" -> "Multi Cap") so the
            # orchestrator's fuzzy matcher resolves the scheme correctly.
            name = _repair_toc_name(name)
            out[page_num] = name
        # Stop after the first page that yielded entries.
        if out:
            return out
    return out


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# PTR label appears on equity / hybrid / index-fund pages.  Hybrid
# schemes (Aggressive Hybrid, Balanced Advantage) print
# ``Annual Equity Portfolio Turnover Ratio: 0.44 times`` with the
# optional ``Equity`` word; equity schemes print
# ``Annual Portfolio Turnover Ratio: 0.50 times``. Both yield a fraction
# in "times" — storage convention is the same fraction, so we pass
# through without unit conversion.
_PTR_TEXT_RE = re.compile(
    r"Annual\s+(?:Equity\s+)?Portfolio\s+Turnover\s+Ratio\s*:\s*(\d+\.\d+)\s*times",
    re.IGNORECASE,
)

# Sentinel: newly-launched schemes (< 1y old) print ``: NA``. Drop
# these rows — there's no meaningful value to record.
_PTR_NA_RE = re.compile(
    r"Annual\s+(?:Equity\s+)?Portfolio\s+Turnover\s+Ratio\s*:\s*NA\b",
    re.IGNORECASE,
)


@register_adapter
class LicAdapter(ManagerAdapter):
    """LIC Mutual Fund factsheet adapter.

    ``amc_slug = 'lic'`` matches ``scheme_master.amc_code`` exactly, so
    no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "lic"
    source_label = "LIC Mutual Fund"

    # -------------------------------------------------------------------
    # URL
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the canonical LIC MF factsheet PDF URL for data month
        ``ym='YYYY-MM'``.

        Path structure:
        ``/assets/downloads/monthly_fact_sheet/{fy}/{publish_mm}/
        lic-mf-factsheet-{dd}{ord}-{Month}-{yyyy}.pdf``
        """
        y, m = map(int, ym.split("-"))
        fy = _fiscal_year_dir(ym)
        _, publish_m = _publish_ym(ym)
        day_ord = _day_with_ordinal(m, y)
        month_name = _MONTH_NAMES[m - 1]
        return (
            "https://www.licmf.com/assets/downloads/monthly_fact_sheet/"
            f"{fy}/{publish_m:02d}/"
            f"lic-mf-factsheet-{day_ord}-{month_name}-{y}.pdf"
        )

    # -------------------------------------------------------------------
    # PTR
    # -------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            toc = _build_toc_map(pdf)
            if not toc:
                log.warning("lic.ptr.toc_empty", pdf=str(pdf_path))
                return
            for page_num, scheme in toc.items():
                # Page numbers in the TOC are 1-based and match the
                # rendered footer page number, which equals the
                # 1-based PDF page index.
                if page_num < 1 or page_num > len(pdf.pages):
                    continue
                page = pdf.pages[page_num - 1]
                text = page.extract_text() or ""
                if _PTR_NA_RE.search(text):
                    # Newly-launched scheme: no value to publish.
                    continue
                m = _PTR_TEXT_RE.search(text)
                if not m:
                    continue
                try:
                    ptr_value = float(m.group(1))
                except ValueError:
                    continue
                # Fail-fast: drop NaN / non-positive.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )

    # -------------------------------------------------------------------
    # -------------------------------------------------------------------
    # Holdings — deferred. LIC's portfolio tables don't print ISINs and
    # the parallel Excel-based ISIN-tagged path (Phase 3.A) covers
    # holdings ingestion. We yield nothing here.
    # -------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
