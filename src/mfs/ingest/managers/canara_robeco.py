"""Canara Robeco Mutual Fund — factsheet adapter (Phase 3.D).

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/canara_robeco/2026-04.pdf``, ~4.16 MB, 62 pages, 27
scheme detail pages on pages 15-41 plus cover / TOC / market reviews /
performance / SIP / IDCW history / glossary / disclaimer).

URL discovery
-------------
Canara Robeco MF's website (``www.canararobeco.com``) is a WordPress SPA.
The combined monthly factsheet PDF is exposed on the public
``/documents/news-insights/factsheets/`` listing page (NOT to be confused
with the macroeconomic ``Monthly Factbook`` which appears under
``/documents/news-insights/monthly-factbook/`` and contains no per-scheme
data). The listing page renders each issue as an anchor whose label is
``Factsheet – <MonthName> <YYYY>`` and whose href points at the canonical
PDF on the same WordPress media library:

    https://www.canararobeco.com/wp-content/uploads/{publish_YYYY}/{publish_MM}/
    <some-filename>.pdf

The filename has drifted historically — ``Final-Factsheet-July-25.pdf``
(2025-07), ``Canara-factsheet-as-on-August-2025.pdf`` (2025-08), and a few
months ago added a ``-1`` suffix to the slug
(``Canara-Robeco-factsheet-as-on-January-2026-1.pdf``). The latest months
follow the stable pattern
``Canara-Robeco-factsheet-as-on-<MonthName>-<YYYY>.pdf`` — but rather than
chase those drifts, ``build_url`` resolves the URL by scraping the
listing page and picking the anchor whose label matches the target data
month. This is robust to filename drift and reuses the public anchor that
Canara Robeco's own UI exposes.

Layout findings driving the parser
----------------------------------
* The combined factsheet has a fixed-position layout: pages 1-14 cover
  the table of contents, market reviews and economic indicators; pages
  15-41 are the per-scheme detail pages (27 schemes); pages 42-62 cover
  performance summaries, IDCW history, glossary, etc.

* Each scheme detail page's FIRST non-empty line is the printed scheme
  name in ALL CAPS — e.g. ``CANARA ROBECO LARGE CAP FUND``. A handful of
  pages decorate the name with a trailing ``*`` footnote marker
  (``CANARA ROBECO LARGE AND MID CAP FUND*``, ``CANARA ROBECO
  CONSUMPTION FUND*``) which we strip. The ``Performance for all
  Schemes`` summary pages have ``Performance`` (not ``CANARA``) as the
  first token so they're naturally filtered out by our anchor regex.

* PTR label: ``Portfolio Turnover Ratio 0.17 times`` on equity / hybrid
  pages. Canara Robeco prints PTR in **"times" = fraction** — so 0.17
  means 17% turnover. No /100 unit conversion is needed; we pass through
  directly into the fractional storage convention.

  Hybrid schemes (Conservative Hybrid p38, Equity Hybrid p39, Balanced
  Advantage p40, Multi Asset Allocation p41) print TWO rows:
      ``Portfolio Turnover Ratio (Equity) 0.10 times``
      ``Portfolio Turnover Ratio 1.35 times``        (or ``(Total)``)
  We prefer the ``(Equity)`` variant (representative of stock-picking
  churn) when present, mirroring the choice made in
  ``franklin.py``/``tata.py``.

* PTR coverage: 16 / 27 detail pages carry a numeric PTR. The 11
  pages without PTR are the pure debt / liquid / overnight / money
  market / gilt / short / corporate / income / dynamic / banking-PSU
  schemes (legitimate — debt funds typically don't publish PTR) plus
  ``CANARA ROBECO BANKING AND FINANCIAL SERVICES FUND`` (page 27),
  which is a brand-new equity scheme launched 20-March-2026 with NAV
  still at ~₹10 — it has no history to compute PTR over. The brief
  targets ≥12 PTR rows from the equity-ranked universe; we ship 16.

* AUM label: ``Month end Assets Under Management (AUM)# ` 16,542.19Crores``
  where ``` ``` is pdfplumber's rendering of the rupee glyph (₹). The
  next line is ``Monthly AVG Assets Under Management (AAUM) `..Crores``
  which we deliberately skip (Stage 2 treats ``aum_crore`` as a
  point-in-time value, so Month-End is preferred over AAUM).

  Two AUM variants are tolerated by the regex: a space between the
  rupee glyph and the value (``` 16,542.19Crores``) OR no space
  (```16,542.19Crores``) — both are observed across the 27 detail
  pages depending on column-width.

* AUM coverage: 27 / 27 detail pages — every scheme prints Month-End
  AUM, including the newly-launched Banking & Financial Services Fund.

* Holdings: deferred. Canara Robeco's portfolio table uses a clean
  two-column layout (Name / Market Cap / % of NAV repeated) and could
  be parsed, but Phase 3.A covers portfolio overlap via the
  parallel ISIN-tagged Excel path. ``parse_holdings`` returns ().

Calibration counts on 2026-04
-----------------------------
* 27 scheme detail pages detected on pages 15-41
* 16 PTR records extracted (11 pure-equity + 1 ELSS + 4 hybrid)
* 27 AUM records extracted (all detail pages)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pdfplumber

from mfs.errors import IngestError
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
    """Publish month = data month + 1 (with year rollover).

    Canara Robeco uploads each issue to ``/wp-content/uploads/{YYYY}/{MM}/``
    where the path reflects the **publish** month, e.g. the April-2026
    factsheet lives under ``2026/05/``.
    """
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return y + 1, 1
    return y, m + 1


_LISTING_URL = "https://www.canararobeco.com/documents/news-insights/factsheets/"

# Anchor on the listing page reads ``Factsheet – <MonthName> <YYYY>`` (the
# dash is an en-dash glyph ``–`` in the HTML). We match either the
# en-dash, em-dash or plain hyphen to be defensive against future re-encodes.
_LISTING_ANCHOR_RE_TPL = (
    r'<a[^>]+href="(https?://[^"]+\.pdf)"[^>]*>\s*'
    r'Factsheet\s*[–—\-]\s*{month}\s+{year}\s*</a>'
)


def _resolve_factsheet_url(ym: str) -> str:
    """Scrape Canara Robeco's listing page and return the canonical PDF URL
    for data month ``ym='YYYY-MM'``.

    Filename conventions have drifted historically (``Final-Factsheet-July-
    25.pdf`` vs ``Canara-factsheet-as-on-August-2025.pdf`` vs
    ``Canara-Robeco-factsheet-as-on-January-2026-1.pdf``), so we don't
    construct the URL directly — we look up the anchor whose visible
    label matches ``Factsheet – <MonthName> <YYYY>``.

    Raises ``IngestError`` if the listing page lacks an entry for the
    target month (e.g. the CMS hasn't published the issue yet).
    """
    import httpx

    y, m = map(int, ym.split("-"))
    month_name = _MONTH_NAMES[m - 1]
    try:
        with httpx.Client(
            timeout=60.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
        ) as c:
            r = c.get(_LISTING_URL)
            r.raise_for_status()
            html = r.text
    except Exception as e:  # noqa: BLE001
        raise IngestError(
            f"canara_robeco: failed to fetch listing page {_LISTING_URL}: {e}"
        ) from e
    pattern = _LISTING_ANCHOR_RE_TPL.format(month=month_name, year=y)
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    if not match:
        raise IngestError(
            f"canara_robeco: no Factsheet anchor for {month_name} {y} on "
            f"{_LISTING_URL} (the CMS may not have published the issue yet)."
        )
    return match.group(1)


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------

# Each detail page's first non-empty line is the printed scheme name in
# ALL CAPS prefixed ``CANARA ROBECO``. Stripped of an optional trailing
# ``*`` footnote marker (Large and Mid Cap, Consumption fund use it).
_CANARA_PREFIX = "CANARA ROBECO"


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from the first non-empty line, or
    ``None`` for non-scheme pages (cover / TOC / market commentary /
    performance summary / glossary).
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.upper().startswith(_CANARA_PREFIX):
        return None
    # Drop trailing footnote markers (* or # or †).
    name = re.sub(r"[\*\#†]+\s*$", "", first).strip()
    # Collapse internal whitespace runs.
    name = re.sub(r"\s+", " ", name)
    return name or None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Canara Robeco prints PTR as a **fraction** ("times") on equity / hybrid
# pages — 0.17 means 17% turnover. Storage convention is the same
# fraction, so we pass through with NO /100 conversion.
#
# Hybrid pages print two rows — we prefer the ``(Equity)`` variant when
# present (mirrors franklin.py / tata.py). The plain ``Portfolio Turnover
# Ratio <num> times`` form (no parenthetical) is the pure-equity fund
# variant on pages 15-26.
_PTR_EQUITY_RE = re.compile(
    r"Portfolio\s+Turnover\s+Ratio\s*\(Equity\)\s+(\d+(?:\.\d+)?)\s*times",
    re.IGNORECASE,
)
_PTR_PLAIN_RE = re.compile(
    r"Portfolio\s+Turnover\s+Ratio\s+(\d+(?:\.\d+)?)\s*times",
    re.IGNORECASE,
)


def _extract_ptr(text: str) -> float | None:
    """Return PTR as a fraction or ``None``.

    Prefers the ``(Equity)`` variant on hybrid pages; otherwise takes the
    plain ``Portfolio Turnover Ratio <num> times`` row that pure-equity
    funds print.
    """
    m = _PTR_EQUITY_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = _PTR_PLAIN_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


@register_adapter
class CanaraRobecoAdapter(ManagerAdapter):
    """Canara Robeco Mutual Fund factsheet adapter.

    ``amc_slug = 'canara_robeco'`` matches ``scheme_master.amc_code``
    directly, so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "canara_robeco"
    source_label = "Canara Robeco Mutual Fund"

    # -------------------------------------------------------------------
    # URL
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the canonical Canara Robeco combined-factsheet PDF URL
        for data month ``ym='YYYY-MM'``.

        The URL is resolved by scraping the public listing page rather
        than constructed from a template, because Canara Robeco's
        filename convention has drifted across months
        (``Final-Factsheet-July-25.pdf`` -> ``Canara-factsheet-as-on-
        August-2025.pdf`` -> ``Canara-Robeco-factsheet-as-on-
        December-2025-1.pdf`` -> ``Canara-Robeco-factsheet-as-on-
        April-2026.pdf``). The listing-page anchor's label
        ``Factsheet – <MonthName> <YYYY>`` is the stable lookup key.
        """
        return _resolve_factsheet_url(ym)

    def fetch(self, ym: str) -> Path:
        """Resolve the data-month factsheet URL via the public listing
        page, then download to the canonical cache path.

        Re-uses an existing cached PDF unconditionally — delete the file
        to force a re-fetch. Overrides the base implementation to avoid
        a second network call (the cache fast-path skips the listing
        scrape entirely).
        """
        from mfs import paths
        from mfs.io.http import download_to

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        url = self.build_url(ym)
        return download_to(url, out)

    # -------------------------------------------------------------------
    # PTR / AUM
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
                ptr_value = _extract_ptr(text)
                if ptr_value is None:
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
    # Holdings — deferred to the parallel ISIN-tagged Excel path.
    # Canara Robeco's factsheet portfolio table is a clean two-column
    # layout but lacks ISINs; Phase 3.A covers overlap from Excel.
    # -------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
