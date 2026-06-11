"""Capitalmind Mutual Fund — factsheet PTR adapter (Phase 3.B).

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/capitalmind/2026-04.pdf``, ~9.2 MB, 42 pages).

URL discovery
-------------
Capitalmind publishes one combined monthly factsheet whose download links
live on a single statutory page: ``https://capitalmindmf.com/factsheet.html``.
The hosted filenames carry an unpredictable CMS hash suffix and an
inconsistent month/year encoding across months, e.g.

    /uploads/Capitalmind_Mutual_Fund_Factsheet_April_2026_e5d5949998.pdf
    /uploads/Capitalmind_Mutual_Fund_Factsheet_March_2026_bb6f8ffd12.pdf
    /uploads/Capitalmind_Factsheet_10_Jan2026_600f395ecc.pdf
    /uploads/CMMF_October_Factsheet_with_Liquid_Fund_Oct25_Shared_...pdf

so ``build_url`` cannot deterministically reconstruct the PDF URL. Instead
``build_url(ym)`` returns the stable index page and ``fetch(ym)`` scrapes it,
selecting the single factsheet link whose filename encodes the *data* month
and year (full name or 3-letter abbreviation; 2- or 4-digit year). ``ym`` is
the data month (2026-04); the file is published in May 2026 but the filename
encodes April, so no publish-month shift is needed in the matcher.

Layout findings driving the parser
----------------------------------
* Each scheme has one **detail page** carrying both a ``Scheme Details`` and a
  ``Quantitative Measures`` block. The printed scheme name is the **first**
  non-empty line of that page (``Capitalmind <name>``). The April 2026 PDF has
  three such detail pages:
    - p24 Capitalmind Flexi Cap Fund                  (PTR 0.2)
    - p31 Capitalmind Multi Asset Allocation Fund     (PTR 0.3)
    - p34 Capitalmind Arbitrage Fund                  (PTR 0.86)
  The Liquid Fund has a detail page but no Quantitative Measures / PTR block
  (it is a debt scheme), so it is legitimately dropped.

* PTR is labelled ``Portfolio Turnover`` inside the Quantitative Measures
  block and is printed as a **FRACTION** (``Portfolio Turnover 0.2`` == 20%
  turnover). Storage is also a fraction, so we pass the value through WITHOUT
  dividing by 100.

* The factsheet ALSO prints ``Portfolio Turnover`` a second time on a separate
  "Portfolio Changes" summary page (p26 for Flexi Cap) where it is a recap
  number, not the canonical scheme stat. We gate strictly on the detail page
  (``Scheme Details`` AND ``Quantitative Measures`` both present) so that recap
  page is never matched and no scheme is double-counted.

* The PTR value reliably sits at the END of its text line on every detail page
  (the two-column layout bleeds left-column text in front of the label, but the
  ``Portfolio Turnover <value>`` token pair stays intact), so a plain text
  regex suffices; no word-position fallback is needed for this AMC.
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

    Capitalmind filenames carry the data month as a full name or 3-letter
    abbreviation and the data year as a 2- or 4-digit token, in varied
    punctuation styles. We require BOTH a month token and a year token with
    non-letter / non-digit boundaries so 'apr' doesn't match inside 'april'
    and '26' doesn't match inside '2026'.
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
    """Return the printed scheme name from a scheme DETAIL page, or None.

    A detail page carries both a ``Scheme Details`` and a ``Quantitative
    Measures`` block, and its first non-empty line is the scheme banner
    ``Capitalmind <name>``. Pages without both blocks (cover, index, CEO
    letter, portfolio holdings, portfolio-changes recap, tax reckoner,
    riskometer) are dropped so the recap page's stray ``Portfolio Turnover``
    is never matched.
    """
    if not text:
        return None
    if "Scheme Details" not in text or "Quantitative Measures" not in text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.startswith("Capitalmind "):
        return None
    first = re.sub(r"\s+", " ", first).strip()
    return first or None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Capitalmind prints PTR as a FRACTION under "Quantitative Measures":
# "Portfolio Turnover 0.2" (== 20% turnover). Storage is also a fraction, so we
# pass the value through WITHOUT dividing by 100. A decimal-or-integer value is
# accepted because single-decimal fractions like 0.2 / 0.3 are printed without a
# trailing zero; the strict detail-page gate prevents capturing the wrong token.
_PTR_TEXT_RE = re.compile(
    r"Portfolio\s+Turnover\s+(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@register_adapter
class CapitalmindAdapter(ManagerAdapter):
    """Capitalmind Mutual Fund factsheet adapter.

    ``amc_slug`` ("capitalmind") matches ``scheme_master.amc_code`` directly,
    so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "capitalmind"
    source_label = "Capitalmind Mutual Fund"

    # -------------------------------------------------------------------
    # URL / fetch
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the stable factsheet index URL for data month ym='YYYY-MM'.

        Capitalmind's hosted PDF filenames carry an unpredictable CMS hash and
        an inconsistent month encoding (see module docstring), so the canonical
        entry point is the public factsheet listing; ``fetch()`` resolves the
        per-month PDF from it. ``ym`` is accepted for interface symmetry with
        deterministic adapters.
        """
        return "https://capitalmindmf.com/factsheet.html"

    def fetch(self, ym: str) -> Path:
        """Download Capitalmind's combined factsheet for data month ym.

        Scrapes the factsheet index page, then selects the single factsheet
        link whose filename encodes the target data month/year. Relative
        ``/uploads/...`` hrefs are resolved against the site origin.

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
        origin = "https://capitalmindmf.com"
        try:
            html = fetch_bytes(page_url).decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            raise IngestError(
                f"capitalmind: failed to fetch index page {page_url}: {e}"
            ) from e

        # Match both absolute and root-relative PDF hrefs.
        raw = re.findall(
            r"(?:https?://[^\"'\s<>\\]+?|/[^\"'\s<>\\]+?)\.pdf",
            html,
            re.IGNORECASE,
        )
        candidates: list[str] = []
        for u in dict.fromkeys(raw):  # dedupe, preserve order
            fn = u.rsplit("/", 1)[-1].lower()
            if "factsheet" not in fn:
                continue
            if "kim" in fn or "sid" in fn or "addendum" in fn:
                continue
            if _factsheet_matches_month(u, year, month):
                full = u if u.startswith("http") else f"{origin}{u}"
                candidates.append(full)

        if not candidates:
            raise IngestError(
                f"capitalmind: no combined factsheet PDF for data month {ym} "
                f"found on {page_url}. The index-page layout or filename "
                "convention may have changed — please re-discover."
            )
        if len(candidates) > 1:
            log.warning("capitalmind.fetch.ambiguous", ym=ym, candidates=candidates)
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
                m = _PTR_TEXT_RE.search(text)
                if not m:
                    continue
                try:
                    ptr_value = float(m.group(1))
                except ValueError:
                    continue
                # Drop NaN / non-positive — Capitalmind prints a real fraction
                # or omits the block; there is no zero-PTR scheme.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                # FRACTION — pass through without dividing by 100.
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )
