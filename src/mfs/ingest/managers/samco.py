"""Samco Mutual Fund — factsheet PTR adapter.

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/samco/2026-04.pdf``, ~4.5 MB, 62 pages). Downloaded
from
``https://www.samcomf.com/amc-document-download/Factsheet_April2026_1777643991.pdf``
(the trailing ``_1777643991`` is the publish-time unix epoch = 2026-05-01,
confirming the publish month is data-month + 1).

URL discovery
-------------
Samco's filenames carry a per-upload unix-timestamp suffix that cannot be
derived from the data month, and the stem itself drifts over time
(``Factsheet_April2026``, ``Factsheet-March2026``,
``SamcoMutualFundFactsheetOctober2024`` …). The one invariant across every
historical filename is that it contains ``...factsheet...<MonthName><Year>...
.pdf`` where ``<MonthName><Year>`` is the **data** month (April2026 = April
data, published in May). So ``build_url`` returns the public downloads
listing page and ``fetch`` scrapes the listing for the PDF link whose
filename contains the data month — mirroring the bandhan adapter's
CMS-permalink discovery pattern.

Layout findings driving the parser
----------------------------------
* Each scheme's first page starts with the bare printed scheme name on the
  first non-empty line, prefixed ``Samco `` (e.g. ``Samco Active Momentum
  Fund``, ``Samco Arbitrage Fund``). Continuation / portfolio pages start
  with ``Portfolio as on ...`` and the back-of-book Fund Performance / SIP
  Performance tables (pages 52-61) repeat the ``Samco <name>`` banner but
  carry no PTR line — they are skipped naturally because the PTR regex
  doesn't match.

* PTR is printed on the scheme's first page as
  ``Portfolio Turnover Ratio : 8.7`` (most pages) or, on a couple of pages,
  the shorter label ``Portfolio Turnover : 1.95``. We accept both.

* UNIT — the printed value is ALREADY a fraction / multiple (1.0 == 100%
  turnover); storage uses the same convention so we PASS THROUGH without
  dividing by 100. Although the "How to Read" glossary loosely calls PTR
  "the percentage of a fund's holdings that have changed", the printed
  numbers are SEBI-standard turnover ratios, not percent values:
      - Samco Arbitrage Fund prints 8.57 (= 857% turnover) — the right
        order of magnitude for an arbitrage fund that churns cash-futures
        positions constantly; 8.57% would be implausibly low.
      - Samco Active Momentum Fund prints 8.7 (= 870%) — consistent with a
        momentum strategy's frequent rebalancing.
      - Samco Flexicap 1.95 (195%), ELSS 1.51 (151%) — sane active-equity
        fractions.
  Dividing by 100 here would be the bug; we do NOT.

* Some funds print no PTR at all (Small Cap p30, Large & Mid Cap p34, Mid
  Cap p43, Overnight p49 — all new schemes). Those pages are dropped
  silently (no PTR line → no record), per the no-half-data invariant.

* Column-bleed is benign for PTR: page 23 (Dynamic Asset Allocation)
  extracts as ``MCoasnta &g eGmoeondts Faeneds S.ervice Tax. Portfolio
  Turnover Ratio : 10.33`` — the regex still anchors on the clean
  ``Portfolio Turnover Ratio : <value>`` tail.

Calibration on 2026-04
----------------------
* 9 PTR records parsed (Active Momentum, Multi Asset, Flexicap, Multi Cap,
  Dynamic Asset Allocation, Special Opportunities, Large Cap, ELSS,
  Arbitrage).

* Holdings are intentionally left to the default (empty) — Samco holdings
  come from the separate Excel path, not this PDF adapter.

* ``amc_slug = 'samco'`` matches ``scheme_master.amc_code`` exactly, so no
  alias is needed in ``_scheme_match.py``.
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

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

_DOWNLOADS_URL = "https://www.samcomf.com/downloads"


def _data_month_name(ym: str) -> str:
    """data month 'YYYY-MM' -> MonthName (e.g. 'April')."""
    _, m = ym.split("-")
    return _MONTH_NAMES[int(m) - 1]


# Samco prints PTR as a FRACTION / multiple already (1.0 == 100% turnover):
# "Portfolio Turnover Ratio : 8.7" or the shorter "Portfolio Turnover : 1.95".
# Storage is the same fraction convention -> PASS THROUGH (do NOT divide by 100).
_PTR_TEXT_RE = re.compile(
    r"Portfolio\s+Turnover(?:\s+Ratio)?\s*:\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# A scheme page's first non-empty line is the bare "Samco <name>" banner.
_SCHEME_NAME_RE = re.compile(r"^Samco\b.+", re.IGNORECASE)

# Samco's factsheet banner prints "Flexicap" glued, but scheme_master records
# "Flexi Cap". The downstream fuzzy matcher uses token_set_ratio, which treats
# the glued "FLEXICAP" as one token and scores only 82 against the two-token
# "FLEXI CAP" — below the 85 acceptance threshold — so the Flexi Cap PTR record
# would be silently dropped. Splitting the token back to "Flexi Cap" (matching
# the holdings adapter's _GLUED_WORD_SPLITS) lifts the match to 100. The "Elss"
# banner casing is already absorbed by the case-insensitive canonicalizer.
_FLEXICAP_RE = re.compile(r"\bflexicap\b", re.IGNORECASE)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from the first non-empty line, or None.

    Scheme first-pages lead with ``Samco <name>``. Portfolio / continuation
    pages lead with ``Portfolio as on ...`` and glossary / strategy pages
    lead with other text, so they return None. Performance-table pages also
    lead with ``Samco <name>`` but carry no PTR line, so they yield no
    record regardless.
    """
    if not text:
        return None
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not _SCHEME_NAME_RE.match(first):
        return None
    # Guard against a continuation banner that prepends 'Portfolio as on'.
    if "as on" in first.lower() or "portfolio" in first.lower():
        return None
    # Un-glue "Flexicap" -> "Flexi Cap" so the fuzzy matcher clears threshold.
    first = _FLEXICAP_RE.sub("Flexi Cap", first)
    return re.sub(r"\s+", " ", first).strip()


@register_adapter
class SamcoAdapter(ManagerAdapter):
    """Samco Mutual Fund factsheet adapter (PTR only)."""

    amc_slug = "samco"
    source_label = "Samco Mutual Fund"

    # -------------------------------------------------------------------
    # URL / fetch
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the public downloads listing page for data month ym.

        Samco factsheet PDFs live at
        ``samcomf.com/amc-document-download/<stem><MonthName><Year>_<epoch>.pdf``
        where ``<stem>`` drifts and ``<epoch>`` is an unpredictable
        publish-time unix timestamp, so the exact PDF URL cannot be built
        deterministically. ``fetch`` resolves the real URL by scraping this
        listing for the link whose filename contains ``<MonthName><Year>``.
        """
        return _DOWNLOADS_URL

    def fetch(self, ym: str) -> Path:
        """Download the data-month factsheet; return local Path.

        Re-uses an existing cached PDF unconditionally — delete the file to
        force a re-fetch. Otherwise scrapes the downloads listing for the
        factsheet PDF whose filename contains the data ``<MonthName><Year>``.
        """
        from mfs import paths
        from mfs.io.http import download_to, fetch_bytes

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out

        listing_url = self.build_url(ym)
        try:
            html = fetch_bytes(listing_url).decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            raise IngestError(
                f"samco: failed to fetch downloads listing {listing_url}: {e}"
            ) from e

        month_name = _data_month_name(ym)
        year = ym.split("-")[0]
        # All factsheet PDFs contain '...factsheet...<MonthName><Year>...pdf'.
        # Require the month name and year to be adjacent (allowing an optional
        # single separator) so 'April2026'/'April-2026'/'April 2026' match but
        # an unrelated 'April' elsewhere in the page does not.
        pdf_re = re.compile(
            r"https?://[^\"'\s<>\\]*?factsheet[^\"'\s<>\\]*?"
            rf"{month_name}[\s_-]?{year}[^\"'\s<>\\]*?\.pdf",
            re.IGNORECASE,
        )
        matches = pdf_re.findall(html)
        # Dedupe while preserving order; prefer the canonical samcomf.com host.
        seen: set[str] = set()
        urls: list[str] = []
        for u in matches:
            if u not in seen:
                seen.add(u)
                urls.append(u)
        if not urls:
            raise IngestError(
                f"samco: no factsheet PDF for {month_name} {year} found on "
                f"{listing_url} (data month {ym}). The listing layout or "
                "filename convention may have changed — please re-discover."
            )
        urls.sort(key=lambda u: 0 if "samcomf.com" in u else 1)
        return download_to(urls[0], out)

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
                # Drop NaN / non-positive — Samco prints a real fraction or
                # omits the block entirely; no zero-PTR scheme exists.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                # Pass through: printed value is already a fraction (no /100).
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )
