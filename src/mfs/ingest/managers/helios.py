"""Helios Mutual Fund — factsheet adapter (Phase 3 PTR coverage).

Calibrated against the April 2026 combined factsheet at
``data/raw/factsheets/helios/2026-04.pdf`` (~1.9 MB, 25 pages). Helios is a
small AMC: the combined factsheet carries one first page + one continuation
page per scheme, followed by aggregated performance / SIP / riskometer
back-matter pages.

URL discovery
-------------
Helios publishes monthly factsheets on its WordPress downloads page
(``https://www.heliosmf.in/downloads/``). The PDFs live under
``wp-content/uploads/<publish_year>/<publish_month>/`` (publish month is the
data month + 1 — the April-2026 data factsheet was uploaded in May 2026), but
the **filename is wildly inconsistent month-over-month**:

    Helios-Mutual-Fund-Factsheet_Apr26-Online.pdf   (Apr 2026)
    Helios-Factsheet_Mar-2026.pdf                   (Mar 2026)
    Helios-Factsheet-February-2026.pdf              (Feb 2026)
    Helios-Mutual-Fund-Factsheet_Dec25.pdf          (Dec 2025)
    Helios-Mutual-Fund-Factsheet-July-25.pdf        (Jul 2025)
    Helios-Mutual-Fund-Factsheet-March-2025.pdf     (Mar 2025)

The only stable invariant is that the filename always contains the **data
month** (full name or 3-letter abbreviation) and the **data year** (2- or
4-digit). Because the exact filename cannot be constructed deterministically,
``build_url(ym)`` returns the stable downloads-page URL (the public route a
human would follow), and ``fetch()`` scrapes that page for the one combined
factsheet link whose filename encodes the target data month/year.

The downloads page also lists a separate per-month *performance-only* PDF
(``Performance-Helios-Factsheet_Apr-26.pdf`` /
``Perfromance_Helios-Factsheet-Mar26.pdf`` — note the recurring "Perfromance"
typo) and per-scheme KIMs; both are excluded so only the combined factsheet
matches.

Layout findings driving the parser
----------------------------------
* Each scheme's first page starts with the printed scheme name on the first
  non-empty line, prefixed ``Helios `` (e.g. ``Helios Flexi Cap Fund``). The
  immediately following page is a continuation page that repeats the same
  header but carries no Quantitative Data block — it yields no PTR, and the
  orchestrator's (scheme_code, as_of_month) PK dedupes harmlessly anyway.

* PTR is printed under a ``Portfolio Turnover* (Times)`` heading as
  ``Equity Turnover 0.22`` — a **fraction** (0.22 == 22% turnover), so we
  pass it through with NO percent→fraction conversion. We deliberately take
  ``Equity Turnover`` and NOT ``Total Turnover``: on the Balanced Advantage
  page the two diverge (Equity 0.42 vs Total 2.92, the latter including the
  derivative/hedge leg), and Equity Turnover is the canonical Phase-2 metric.

* The 2026-04 PDF yields 7 PTR rows — the 6 ranked equity funds (Flexi Cap,
  Financial Services, Large & Mid Cap, Mid Cap, Small Cap, Balanced
  Advantage) plus the Arbitrage Fund. The Overnight (debt) fund and all
  performance/SIP aggregation pages legitimately omit the Equity Turnover
  block and are skipped by the regex gate.

* Two-strategy PTR extraction: a text regex (fast path, clean on every 2026-04
  scheme page) plus a word-position fallback for future months where the
  right-hand holdings table might column-bleed into the Quantitative Data
  block.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pdfplumber

from mfs.errors import IngestError
from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)


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

    Helios filenames carry the data month as a full name or 3-letter
    abbreviation and the data year as a 2- or 4-digit token, in any of a
    dozen punctuation styles. We require BOTH a month token and a year token
    with non-letter / non-digit boundaries so 'mar' doesn't match inside
    'march' twice and '25' doesn't match inside '2025'.
    """
    fn = url.rsplit("/", 1)[-1].lower()
    full = _MONTH_FULL[month - 1]
    abbr = _MONTH_ABBR[month - 1]
    if not re.search(rf"(?<![a-z])(?:{full}|{abbr})(?![a-z])", fn):
        return False
    y4 = str(year)
    y2 = f"{year % 100:02d}"
    return re.search(rf"(?<!\d)(?:{y4}|{y2})(?!\d)", fn) is not None


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from the first non-empty line, or None.

    A scheme page's first non-empty line is ``Helios <name>``. Non-scheme
    pages (cover ``FACTSHEET``, ``Index``, ``Scheme Performance``,
    ``Riskometer`` etc.) don't start with ``Helios `` and are dropped. The
    aggregated SIP / performance pages that DO start with a scheme name carry
    no Equity Turnover block, so they never produce a PTR record regardless.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.startswith("Helios "):
        return None
    first = re.sub(r"\s+", " ", first).strip()
    return first or None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Helios prints PTR as a FRACTION under "Portfolio Turnover* (Times)":
# "Equity Turnover 0.22" (== 22% turnover). Storage is also a fraction, so we
# pass through WITHOUT dividing by 100. We anchor on "Equity Turnover" (not
# "Total Turnover", which adds the derivative leg and diverges on hybrid
# funds) and require a decimal value so a stray integer can't be captured.
_PTR_TEXT_RE = re.compile(r"Equity\s+Turnover\s+(\d+\.\d+)", re.IGNORECASE)


def _find_ptr_via_words(page) -> float | None:
    """Word-position fallback for PTR extraction.

    Locate the ``Equity`` + ``Turnover`` word pair in the left column
    (x0 < 250) on the same y-band, then take the first ``\\d+\\.\\d+`` token
    to the right of ``Turnover`` on that tight band (±2pt). The tight band
    avoids pulling the ``Total Turnover`` value from the next visual line.
    """
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None
    for i, w in enumerate(words):
        if w["text"] != "Equity" or w["x0"] >= 250:
            continue
        turnover: dict | None = None
        for j in range(i + 1, min(i + 6, len(words))):
            v = words[j]
            if abs(v["bottom"] - w["bottom"]) > 2:
                continue
            if v["x0"] <= w["x1"] or v["x0"] >= 250:
                continue
            if v["text"].lower().startswith("turnover"):
                turnover = v
                break
        if turnover is None:
            continue
        nums = [
            v for v in words
            if abs(v["bottom"] - turnover["bottom"]) <= 2
            and v["x0"] > turnover["x1"]
            and re.fullmatch(r"\d+\.\d{1,3}", v["text"].rstrip(",;."))
        ]
        nums.sort(key=lambda v: v["x0"])
        for v in nums:
            try:
                val = float(v["text"].rstrip(",;."))
            except ValueError:
                continue
            if val > 0:
                return val
        return None
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@register_adapter
class HeliosAdapter(ManagerAdapter):
    """Helios Mutual Fund factsheet adapter.

    ``amc_slug`` ("helios") matches ``scheme_master.amc_code`` directly, so no
    alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "helios"
    source_label = "Helios Mutual Fund"

    # -------------------------------------------------------------------
    # URL / fetch
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the stable downloads-page URL for data month ym='YYYY-MM'.

        Helios factsheet filenames are not deterministic month-over-month
        (see module docstring), so the canonical entry point is the public
        downloads listing; ``fetch()`` resolves the per-month PDF from it.
        ``ym`` is accepted for interface symmetry with deterministic adapters.
        """
        return "https://www.heliosmf.in/downloads/"

    def fetch(self, ym: str) -> Path:
        """Download Helios's combined factsheet for data month ym.

        Scrapes the downloads page, then selects the single combined
        factsheet link whose filename encodes the target data month/year
        (excluding the separate performance-only PDF and per-scheme KIMs).

        Re-uses an existing cached PDF unconditionally — delete the file to
        force a re-fetch.
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
                f"helios: failed to fetch downloads page {page_url}: {e}"
            ) from e

        urls = re.findall(r"https?://[^\"'\s<>\\]+?\.pdf", html, re.IGNORECASE)
        candidates: list[str] = []
        for u in dict.fromkeys(urls):  # dedupe, preserve order
            fn = u.rsplit("/", 1)[-1].lower()
            if "factsheet" not in fn:
                continue
            if "kim" in fn:
                continue
            # Exclude the separate performance-only doc (recurring
            # "Perfromance" typo on the site is handled by the "perf" stem).
            if "perf" in fn:
                continue
            if _factsheet_matches_month(u, year, month):
                candidates.append(u)

        if not candidates:
            raise IngestError(
                f"helios: no combined factsheet PDF for data month {ym} found "
                f"on {page_url}. The downloads-page layout or filename "
                "convention may have changed — please re-discover."
            )
        if len(candidates) > 1:
            log.warning(
                "helios.fetch.ambiguous", ym=ym, candidates=candidates,
            )
        return download_to(candidates[0], out)

    # -------------------------------------------------------------------
    # PTR
    # -------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                # Strategy 1: text regex (fast path).
                ptr_value: float | None = None
                m = _PTR_TEXT_RE.search(text)
                if m:
                    try:
                        ptr_value = float(m.group(1))
                    except ValueError:
                        ptr_value = None
                # Strategy 2: word-position fallback if the label is present
                # but extract_text column-bled the value.
                if ptr_value is None and "Turnover" in text:
                    ptr_value = _find_ptr_via_words(page)
                if ptr_value is None:
                    continue
                # Fail-fast: drop NaN / non-positive. Helios prints a real
                # fraction or omits the block; there is no zero-PTR scheme.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,  # already a fraction — no /100
                    source_amc=self.amc_slug,
                )

    # parse_holdings: intentionally left as the base default (holdings come
    # from the separate Excel path, not this factsheet adapter).
    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
