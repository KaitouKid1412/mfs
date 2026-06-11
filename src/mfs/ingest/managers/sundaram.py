"""Sundaram Mutual Fund — factsheet adapter (Phase 3).

Calibrated against the April 2026 consolidated factsheet PDF
(``data/raw/factsheets/sundaram/2026-04.pdf``, ~12.4 MB, 75 pages, 36
scheme pages followed by IDCW history / disclosures / performance track
record / riskometer / fund-manager tables).

URL discovery
-------------
``www.sundarammutual.com`` is an ASP.NET WebForms site. The "Fundwise
Factsheet" page (``/fundwise-factsheet``) hosts a "Consolidated
Factsheet" download whose archive endpoint is exposed via the cached
script at ``/ajax/Modules_Forms_Downloads_Fundwise_Factsheet,App_Web_<hash>.ashx``.
Calling that handler with ``_method=DownloadArchive`` and a body
``cat=1\\r\\nmnth=MM/YYYY`` returns a one-line response of the form

    'https://www.sundarammutual.com/uploaddir/consolidated_factsheet/
     Consolidated_Factsheet_<M>_<YYYY>_<DDMMYY>_<HHMMSS>.pdf'

The URL embeds the upload timestamp, so the filename is NOT
deterministic month over month — a fresh API call is required to
resolve the canonical PDF URL.

``build_url(ym)`` returns the deterministic ASHX endpoint URL (stable
across months). ``fetch(ym)`` overrides the default downloader to POST
the per-month form, parse the single-quoted URL out of the response,
and download the resulting PDF.

Layout findings driving the parser
----------------------------------
* Every active scheme page starts with a first non-empty line of the
  form ``Sundaram <Name> Fund`` (or ``... FoF`` for the global brand
  FoF). The same prefix appears on continuation pages (which carry only
  the holdings continuation table), so the FUND FEATURES block is used
  as a positive filter — continuation pages omit it.

* PTR is printed in the bottom of the same left FUND FEATURES column
  as:

      ``Turnover Ratio 31.4``

  Sundaram prints PTR as a **percent** (Sundaram Large Cap Fund =
  ``31.4`` i.e. 31.4%), so the adapter divides by 100 to keep the
  fraction storage convention. Some pages have right-column portfolio
  rows fused on the same visual line (e.g. p8: ``Turnover Ratio 37.2
  Berger Paints Ltd 0.4 Telecom - Services 2.1``), but the regex
  anchors on the numeric token immediately after the label so the
  trailing portfolio fragments don't interfere.

* Pure-debt / FoF / Multi Asset Allocation pages legitimately don't
  print Turnover Ratio (their portfolios aren't churn-driven in a way
  comparable to equity schemes). 21/36 scheme pages carry PTR; the
  orchestrator's PK-dedupe and asymmetric-coverage handling makes this
  fine.

* Pages 40-41 are closed-end ELSS Series funds (``Sundaram Long Term
  Tax Advantage Fund Series III-IV`` and ``Sundaram Long Term Micro
  Cap Tax Advantage Fund - Series III - VI``). These print
  Turnover in a per-series tabular layout (``Turnover Ratio (%) 19
  20``), which doesn't map cleanly to a single (scheme, value) tuple.
  The active universe Phase 2 tracks doesn't include these
  closed-ended series, so per the fail-fast invariant we drop them
  rather than fabricate a representative value.

* Pages 1-6 (cover / index / market outlook / how-to-read), 55-75
  (IDCW history / disclosures / track record / riskometer / fund
  managers / blank) have first lines that don't start with
  ``Sundaram `` and so naturally produce no rows.

* Some scheme names carry a "Formerly known as Principal ..." note in
  the disclosures section (Sundaram acquired the Principal AMC
  business in 2022). The first-line scheme header in the FUND FEATURES
  block is always the Sundaram-branded name; no "Principal" prefix
  appears at the top of any scheme page.

Calibration counts on 2026-04
-----------------------------
* 36 active scheme pages detected (pp 7-39, excluding continuation
  pages)
* 21 PTR rows extracted (equity / hybrid / arbitrage / index pages —
  pure debt / FoF / MAA legitimately omit the Turnover Ratio block)
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


# Sundaram first-line prefix on every active scheme page. The FoF scheme
# (Sundaram Global Brand Theme - Equity Active FoF) uses the same prefix.
_SCHEME_PREFIX = "Sundaram "

# Sundaram's factsheet spells two flagship funds differently from AMFI's
# scheme_master recorded name, and the divergence breaks the shared fuzzy
# matcher (token_set_ratio + Levenshtein tie-break):
#   - "Sundaram Flexi Cap Fund"          -> mis-matched to "Sundaram Mid Cap
#     Fund" (the split "Flexi Cap" loses the single-token "FLEXICAP" signal).
#   - "Sundaram Large and Mid Cap Fund"  -> mis-matched to "Sundaram Large Cap
#     Fund" at a perfect token_set_ratio of 100 ("Large Cap" is a pure token
#     subset, and "MID CAP" != master's single-token "MIDCAP").
# We can't touch the shared matcher; instead we rewrite the printed name we
# emit so it lands on the master's spelling. Pure rename, no value change.
_NAME_NORMALIZE = {
    "sundaram flexi cap fund": "Sundaram Flexicap Fund",
    "sundaram large and mid cap fund": "Sundaram Large and Midcap Fund",
}


def _normalize_scheme_name(name: str) -> str:
    """Map a factsheet scheme header to the AMFI-spelled name when the two
    diverge in a way that breaks the shared fuzzy matcher; otherwise return
    the name unchanged."""
    key = re.sub(r"\s+", " ", name).strip().lower()
    return _NAME_NORMALIZE.get(key, name)


# PTR (text-extract path). Matches ``Turnover Ratio 31.4`` — Sundaram
# stores PTR as a percent (i.e. 31.4 means 31.4%, not 3140%). The regex
# tolerates extra right-column portfolio fragments fused onto the line
# (e.g. ``Turnover Ratio 37.2 Berger Paints Ltd 0.4``).
_PTR_RE = re.compile(
    r"Turnover\s+Ratio\s+(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme title from the first non-empty line, or
    None for non-scheme pages (cover / index / outlook / how-to-read /
    annexure / disclosures / IDCW history / track record / riskometer /
    fund managers).

    Returns None for continuation pages too — they start with
    ``Sundaram XXX Fund`` but omit the FUND FEATURES block, so the
    caller additionally requires that marker before treating the page
    as a scheme page.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.startswith(_SCHEME_PREFIX):
        return None
    # Collapse any internal multi-whitespace runs (defensive — none
    # observed in 2026-04 but cheap insurance).
    first = re.sub(r"\s+", " ", first).strip()
    return first or None


def _resolve_factsheet_url(ym: str) -> str:
    """Resolve the consolidated-factsheet PDF URL for data month ``ym``.

    Sundaram's ASHX archive endpoint takes a body of
    ``cat=1\\r\\nmnth=MM/YYYY`` and returns a single-quoted absolute URL
    string. ``cat=1`` is the "Consolidated Factsheet" category; other
    cat values (2-5) correspond to per-fund factsheet variants that
    Sundaram doesn't currently publish ("Not Available").

    Returns the absolute PDF URL. Raises ``IngestError`` if the API
    response is "Not Available" or doesn't parse as a quoted URL,
    consistent with the fail-fast invariant.
    """
    import httpx

    y, m = map(int, ym.split("-"))
    api_url = (
        "https://www.sundarammutual.com/ajax/"
        "Modules_Forms_Downloads_Fundwise_Factsheet,App_Web_jmhjkqpp.ashx"
        "?_method=DownloadArchive&_session=no"
    )
    body = f"cat=1\r\nmnth={m:02d}/{y:04d}"
    try:
        with httpx.Client(
            timeout=60.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.sundarammutual.com/fundwise-factsheet",
                "X-Requested-With": "XMLHttpRequest",
            },
            follow_redirects=True,
        ) as c:
            r = c.post(api_url, content=body)
            r.raise_for_status()
            payload = r.text.strip()
    except Exception as e:  # noqa: BLE001
        raise IngestError(
            f"sundaram: archive API failed at {api_url} for {ym}: {e}"
        ) from e
    if "Not Available" in payload:
        raise IngestError(
            f"sundaram: archive API returned 'Not Available' for data "
            f"month {ym}. The consolidated factsheet may not have been "
            "published yet."
        )
    m_url = re.search(r"https?://[^\s'\"<>]+\.pdf", payload, re.IGNORECASE)
    if not m_url:
        raise IngestError(
            f"sundaram: archive API response did not contain a PDF URL "
            f"for {ym}: {payload[:200]!r}"
        )
    return m_url.group(0)


@register_adapter
class SundaramAdapter(ManagerAdapter):
    """Sundaram Mutual Fund factsheet adapter.

    ``amc_slug = 'sundaram'`` matches ``scheme_master.amc_code``
    directly, so no alias entry in ``_scheme_match.py`` is required.

    Sundaram acquired the Principal AMC India business in 2022, so a
    handful of schemes still carry a "Formerly Known as Principal ..."
    note in the disclosures section. The top-of-page scheme header is
    always the Sundaram-branded name, so no special handling is needed
    in ``_scheme_name_from_page``.
    """

    amc_slug = "sundaram"
    source_label = "Sundaram Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the deterministic Sundaram archive ASHX URL.

        Sundaram embeds an upload timestamp in its actual PDF filename
        (``Consolidated_Factsheet_<M>_<YYYY>_<DDMMYY>_<HHMMSS>.pdf``), so
        the canonical PDF URL can only be resolved via the archive API.
        ``build_url`` returns the stable API endpoint URL; ``fetch``
        does the POST-and-resolve dance.
        """
        # The endpoint is the same regardless of ym; we still validate
        # ym so calls with malformed data months fail early.
        y, m = map(int, ym.split("-"))
        if not (1 <= m <= 12):
            raise IngestError(f"sundaram: invalid data month {ym!r}")
        if y < 2000:
            raise IngestError(f"sundaram: implausible data year {ym!r}")
        return (
            "https://www.sundarammutual.com/ajax/"
            "Modules_Forms_Downloads_Fundwise_Factsheet,App_Web_jmhjkqpp.ashx"
            "?_method=DownloadArchive&_session=no"
        )

    def fetch(self, ym: str) -> Path:
        """Resolve the per-month consolidated factsheet URL via the
        archive API, then download to the canonical cache path.

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
                # Require FUND FEATURES marker — filters out the
                # holdings-continuation pages that share the same
                # first-line scheme name.
                if "FUND FEATURES" not in text:
                    continue
                m = _PTR_RE.search(text)
                if not m:
                    # Debt / FoF / Multi Asset Allocation pages
                    # legitimately omit the Turnover Ratio block.
                    continue
                try:
                    pct = float(m.group(1))
                except ValueError:
                    continue
                # Drop NaN / non-positive (Sundaram prints a real number
                # or omits the block — zero never appears in practice).
                if pct != pct or pct <= 0:
                    continue
                # Sundaram prints PTR as a percent; storage convention
                # is fraction (31.4% -> 0.314).
                yield ParsedPtrRecord(
                    scheme_name_printed=_normalize_scheme_name(scheme),
                    ptr=pct / 100.0,
                    source_amc=self.amc_slug,
                )

    # ------------------------------------------------------------------
    # Holdings — deferred. Sundaram's portfolio table is a two-column
    # ``Portfolio / % Of Net Asset`` layout with sector banners
    # interleaved between rows. The factsheet PDF lacks ISINs, so a
    # holdings parser here would only feed the ISIN-less path that
    # Phase 2.2 already deprecated. Phase 3.C's ISIN-tagged Excel ingest
    # is the right path for portfolio coverage.
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
