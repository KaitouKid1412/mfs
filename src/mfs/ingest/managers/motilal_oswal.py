"""Motilal Oswal Mutual Fund — factsheet adapter (Phase 3).

Calibrated against the April 2026 "Most Factsheet April 2026 Active"
combined factsheet PDF (~5.41 MB, 50 pages) hosted at:
  https://www.motilaloswalmf.com/content/dam/motilal-mf/downloads/mf/
    factsheet/2026/may/Most Factsheet April 2026 Active.pdf

URL discovery
-------------
The Motilal Oswal site at ``www.motilaloswalmf.com`` is an AEM Edge
Delivery Services SPA. The Downloads → Fact Sheets section
(``/downloads/factsheets``) loads its document table from a backend
search API discovered by grepping the ``our-funds-block.js`` block JS:

    GET https://www.motilaloswalmf.com/content/aem-cloud-dept-backend-
        motilal-oswal/api/search-documents.json
        ?year=YYYY&category=factsheet&month=<mon>&type=mf

The response is a JSON dict with ``results`` — a list of ``{path, title,
year, month, category, publishDate, mimeType}`` entries. ``path`` is the
``/content/dam/motilal-mf/...`` PDF URL relative to the site origin. The
combined ACTIVE factsheet titled e.g. "factsheet April 2026 active"
publishes in May (one month after the data month); the per-month file
sits under ``/content/dam/motilal-mf/downloads/mf/factsheet/<YYYY>/<mon>/``
where ``<mon>`` is the publish month (data_ym + 1).

Filenames in the DAM are NOT deterministic — Motilal Oswal uses the
human-readable title verbatim with spaces, occasionally prefixed with
"Most " (e.g. "Most Factsheet April 2026 Active.pdf"). The search API
is therefore the only reliable resolver and is the URL exposed by
``build_url``; ``fetch`` calls the API, picks the matching entry, and
downloads the actual PDF behind ``path``.

Layout findings driving the parser
----------------------------------
* Every scheme page's first non-empty line is ``Motilal Oswal <Scheme
  Name> Fund`` (or Plan / ETF for the passive lineup, which is
  published in a separate PDF and not parsed here). 23 active scheme
  pages span pp 4-26 in the April 2026 issue; pp 1-3 are cover / NFO
  notice / Monthly Market Outlook, and pp 27-50 are performance tables,
  rolling returns, AUM disclosures, SIP performance, TER, and other
  cross-scheme cross-references whose first line does NOT start with
  "Motilal Oswal <...> Fund".

* PTR is printed as a single visual line in the Quantitative Indicators
  block:

      ``Portfolio Turnover Ratio 0.61``

  Storage convention is fraction — Motilal Oswal Large Cap Fund prints
  ``0.61`` (61%) directly, NOT as a percent. We pass through as-is.
  Pure-debt / Liquid / Financial Services Fund pages legitimately don't
  print the PTR block. Arbitrage Fund prints ``11.06`` (1,106%) which
  is real (typical for arbitrage strategies that constantly roll
  futures positions).

Calibration counts on 2026-04
-----------------------------
* 23 scheme pages detected (pp 4-26 of the Active factsheet)
* 22 PTR rows (Financial Services Fund p22 has no Performance section
  printed in this issue, so no PTR line)
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


_MONTH_SHORT_LOWER = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


def _publish_ym(data_ym: str) -> str:
    """data month YYYY-MM -> publish month YYYY-MM (data + 1, with rollover)."""
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{m + 1:02d}"


# Detect the first-line scheme title. Motilal Oswal prints every scheme
# page with a first non-empty line of the form ``Motilal Oswal <Name> Fund``
# (Plan / ETF for the passive lineup). Cover / NFO notice / outlook /
# performance tables / disclosure pages do NOT start with this prefix.
_SCHEME_LINE_RE = re.compile(
    r"^(Motilal Oswal\b[^\n]*?\b(?:Fund|Plan|ETF))(?:\s|$)",
    re.IGNORECASE,
)

# PTR — Motilal already prints a fraction (0.61, 1.33, ...). Pass through.
_PTR_RE = re.compile(
    r"Portfolio\s+Turnover\s+Ratio\s+([\-]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme title from a scheme page, or None.

    Motilal Oswal's scheme pages all start with a first non-empty line of
    the form ``Motilal Oswal <Name> Fund``. Cover / market-outlook /
    cross-scheme performance / disclosure pages start with other strings
    and yield None.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    m = _SCHEME_LINE_RE.match(first)
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group(1)).strip()
    return name or None


def _resolve_factsheet_url(ym: str) -> str:
    """Resolve the Motilal Oswal active factsheet PDF URL for data month
    ``ym`` via the AEM search-documents API.

    Motilal stores factsheet PDFs in
    ``/content/dam/motilal-mf/downloads/mf/factsheet/<YYYY>/<mon>/`` where
    ``<mon>`` is the publish month (data_ym + 1). Filenames embed the
    data month in human-readable form (e.g. "Most Factsheet April 2026
    Active.pdf") and are NOT deterministic — they sometimes get a
    "Most " prefix, sometimes not. The search API is the only stable
    resolver. Returns the absolute PDF URL.

    Raises ``IngestError`` if no entry matches the target month+year,
    consistent with the fail-fast invariant.
    """
    import httpx

    data_y, data_m = map(int, ym.split("-"))
    publish_y, publish_m = map(int, _publish_ym(ym).split("-"))
    publish_month_short = _MONTH_SHORT_LOWER[publish_m - 1]
    data_month_short = _MONTH_SHORT_LOWER[data_m - 1]
    api_url = (
        "https://www.motilaloswalmf.com/content/"
        "aem-cloud-dept-backend-motilal-oswal/api/search-documents.json"
        f"?year={publish_y:04d}&category=factsheet"
        f"&month={publish_month_short}&type=mf"
    )
    try:
        with httpx.Client(
            timeout=60.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, */*",
            },
            follow_redirects=True,
        ) as c:
            r = c.get(api_url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        raise IngestError(
            f"motilal_oswal: search-documents API failed at {api_url}: {e}"
        ) from e
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        raise IngestError(
            f"motilal_oswal: search-documents API returned no factsheets for "
            f"publish month {publish_month_short} {publish_y}."
        )
    # The Active combined factsheet is the entry whose title contains
    # the data month name + "active" (case-insensitive). The Passive
    # ETF/index file lives at the same path with the same publish slot
    # but does not carry the active equity / hybrid / debt schemes
    # that drive Stage 2 metrics.
    full_data_month = (
        "January February March April May June July August September October November December".split()
    )[data_m - 1]
    needle_month = full_data_month.lower()
    for entry in results:
        if not isinstance(entry, dict):
            continue
        title = (entry.get("title") or "").lower()
        path = entry.get("path") or ""
        if needle_month in title and "active" in title and path:
            # Path is site-relative; the DAM is served off the same
            # origin as the SPA, so prefix with the site host.
            if path.startswith("http"):
                return path
            return f"https://www.motilaloswalmf.com{path}"
    raise IngestError(
        f"motilal_oswal: no 'active' factsheet entry for {full_data_month} "
        f"{data_y} found in the search-documents API "
        f"({len(results)} entries scanned)."
    )


@register_adapter
class MotilalOswalAdapter(ManagerAdapter):
    """Motilal Oswal Mutual Fund factsheet adapter.

    ``amc_slug = 'motilal_oswal'`` matches ``scheme_master.amc_code``
    directly, so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "motilal_oswal"
    source_label = "Motilal Oswal Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the deterministic AEM search-documents API URL for data
        month ``ym='YYYY-MM'``.

        The actual per-month factsheet PDF URL is not deterministic
        (Motilal Oswal's CMS uses human-readable filenames that
        occasionally carry a "Most " prefix). ``build_url`` returns the
        stable API endpoint; ``fetch`` resolves the per-month PDF path
        from the API response.
        """
        y, _ = map(int, ym.split("-"))
        publish_y, publish_m = map(int, _publish_ym(ym).split("-"))
        publish_month_short = _MONTH_SHORT_LOWER[publish_m - 1]
        return (
            "https://www.motilaloswalmf.com/content/"
            "aem-cloud-dept-backend-motilal-oswal/api/search-documents.json"
            f"?year={publish_y:04d}&category=factsheet"
            f"&month={publish_month_short}&type=mf"
        )

    def fetch(self, ym: str) -> Path:
        """Resolve the data-month factsheet URL via the search-documents
        API, then download to the canonical cache path.

        Re-uses an existing cached PDF unconditionally — delete the
        file to force a re-fetch.
        """
        from mfs import paths
        from mfs.io.http import download_to

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        pdf_url = _resolve_factsheet_url(ym)
        return download_to(pdf_url, out)

    # ------------------------------------------------------------------
    # PTR
    # ------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                m = _PTR_RE.search(text)
                if not m:
                    continue
                try:
                    ptr = float(m.group(1))
                except ValueError:
                    continue
                # Drop NaN / non-positive. Motilal prints a real fraction
                # or omits the block; zero never appears in practice.
                if ptr != ptr or ptr <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr,
                    source_amc=self.amc_slug,
                )

    # ------------------------------------------------------------------
    # Holdings — deferred. Motilal's portfolio tables use the same
    # two-column factsheet layout as the other Phase-3 adapters and the
    # PDF lacks ISINs. Phase 3.C provides the parallel ISIN-tagged Excel
    # path.
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
