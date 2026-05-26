"""Union Mutual Fund — factsheet adapter (Phase 3.B).

Calibrated against the April 2026 combined factsheet PDF
(``data/raw/factsheets/union/2026-04.pdf``, ~3.13 MB, 56 pages, 32 scheme
pages — 18 equity + 5 hybrid + 9 debt + 2 FoF — preceded by a cover
glossary, market review, "Year on Year Leaders" tables and followed by
NAV / Funds at a Glance / SIP performance / scheme details / IDCW history
/ disclosures at the back).

URL discovery
-------------
``www.unionmf.com`` is a Sitefinity 14 SPA whose factsheet downloads page
at ``/about-us/downloads#factsheets`` is rendered client-side by an
AngularJS controller (``myCtrlFactsheets``). The controller queries the
Sitefinity OData API endpoint

    https://www.unionmf.com/api/downloads/documents
        ?$filter=FolderId eq 04ec7e02-4df3-424f-b99d-0ef4c3800f3e

filters the response on the literal ``Title`` (e.g. ``"April 2026"``),
and uses the ``Url`` field which embeds a non-deterministic ``sfvrsn``
query-string cache buster (e.g. ``factsheet-april-2026.pdf?sfvrsn=8d70a2f8_5``).
The filename slug is also inconsistent across months -- April 2026 is
``factsheet-april-2026.pdf`` but May 2025 is ``factsheet---may-2025.pdf``
(extra dashes), August 2024 is ``august-2024.pdf`` (no ``factsheet-``
prefix), and 2024-Q1 used ``union-mf-factsheet_january-2024.pdf`` (older
filename convention with underscore + Title Case month). Neither the
filename nor the ``sfvrsn`` token can be predicted, so ``build_url``
returns the **API endpoint** (which IS deterministic and stable across
months) and ``fetch`` resolves the per-month PDF URL by querying the
API and matching on ``Title``.

Layout findings driving the parser
----------------------------------
* Each scheme has a single dedicated detail page (pages 6-37). The
  scheme title appears in a left-column header band at top y < 100,
  rendered as the word ``Union`` (separately) followed by an all-caps
  fund name token group on one or two lines, e.g.::

      Union
      FLEXI CAP FUND        (top=~76)

  Multi-line titles wrap as::

      Union
      INNOVATION &          (top=~74)
      OPPORTUNITIES FUND    (top=~88)

  ``pdfplumber.extract_text`` interleaves these tokens with the
  riskometer / "this product is suitable" disclosure that bleeds in
  from the right column, so the text dump first-line is uselessly
  ``"This"`` or ``"Union"``. Word-position extraction with a strict
  bounding box (top < 100, x0 < 200) recovers the title cleanly. A
  small stop-token list excludes the ``NSE/ BSE Symbol: UNIONGOLD``
  line that bleeds into the title region only on the Gold ETF page.

* PTR is printed in the right column of the left page-half (x ~ 380-580)
  in a "Quantitative Indicators - Growth Option" block with the label
  ``Portfolio Turnover Ratio$$$``. The label spans two lines on most
  equity pages (``Portfolio`` / ``Turnover Ratio$$$``) with the numeric
  value on the very next line tagged as ``X.XX times``. A handful of
  pages render it inline as ``Portfolio Turnover Ratio$$$ : 1.34 times``.
  Both shapes are captured by a single regex anchored on the label
  ``Turnover Ratio`` plus a tolerant span that skips the intermediate
  Std-Dev / Sharpe / Beta numeric tokens before grabbing the
  ``X.XX times`` value. Union prints PTR in **times** (already a
  fraction — 0.77 = 77%), so we pass through without unit conversion.
  Debt schemes (pages 30-37) and Fund-of-Fund schemes (pages 25-27)
  legitimately omit the PTR block.

* Pages 1-5 (cover / glossary / market review / "Year on Year Leaders"
  tables) and 38+ (NAV table / SIP performance / scheme details /
  IDCW / disclosures) do NOT carry the scheme-detail layout, so the
  scheme-title detection naturally filters them out.

Calibration counts on 2026-04
-----------------------------
* 32 scheme pages detected (18 equity + 5 hybrid + 9 debt + 2 FoF)
* 21 PTR rows extracted (equity + hybrid + arbitrage + gold ETF;
  debt schemes and FoF schemes legitimately omit the PTR block)

Holdings extraction is deferred to the parallel Excel-based ISIN-tagged
path (Phase 3.C) — the factsheet portfolio table prints company name +
weight but no ISIN, so it can't satisfy the (scheme_code, isin, weight)
contract on its own.
"""

from __future__ import annotations

import json
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

# Sitefinity folder ID for the Factsheets document library on
# unionmf.com. Stable since 2019 (verified against the API response). If
# the AMC ever migrates off Sitefinity this constant must be re-checked.
_FACTSHEETS_FOLDER_ID = "04ec7e02-4df3-424f-b99d-0ef4c3800f3e"
_API_URL = (
    "https://www.unionmf.com/api/downloads/documents?$filter=FolderId%20eq%20"
    + _FACTSHEETS_FOLDER_ID
)
_SITE_BASE = "https://www.unionmf.com"


# ---------------------------------------------------------------------------
# URL resolution (per-month PDF URL is dynamic)
# ---------------------------------------------------------------------------


def _resolve_factsheet_url(ym: str) -> str:
    """Query Union's Sitefinity downloads API and return the absolute PDF
    URL for the data month ``ym='YYYY-MM'``.

    The API returns a JSON object with ``value`` = list of document entries,
    each carrying ``Title`` (e.g. ``"April 2026"``) and ``Url`` (relative,
    e.g. ``/docs/default-source/.../factsheet-april-2026.pdf?sfvrsn=...``).
    The ``Url`` field embeds a non-deterministic ``sfvrsn`` cache-buster
    that changes whenever the file is re-uploaded; the filename slug also
    varies across months (factsheet-april-2026 vs factsheet---may-2025 vs
    august-2024 vs union-mf-factsheet_april-2024 historically), so we
    cannot construct the URL directly — the API is the only stable source.

    Raises IngestError if the API returns no match — we never fall back
    to a hand-rolled URL guess.
    """
    import httpx

    y, m = map(int, ym.split("-"))
    month_name = _MONTH_NAMES[m - 1]
    expected_title = f"{month_name} {y}"
    try:
        with httpx.Client(
            timeout=60.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
            follow_redirects=True,
        ) as c:
            r = c.get(_API_URL)
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, json.JSONDecodeError) as e:  # noqa: BLE001
        raise IngestError(
            f"union: factsheet API request failed at {_API_URL}: {e}"
        ) from e
    docs = data.get("value") or []
    for d in docs:
        if (d.get("FolderId") or "") != _FACTSHEETS_FOLDER_ID:
            continue
        title = (d.get("Title") or "").strip()
        if title != expected_title:
            continue
        url = (d.get("Url") or "").strip()
        if not url:
            continue
        if url.startswith("/"):
            return _SITE_BASE + url
        return url
    raise IngestError(
        f"union: no factsheet entry with Title={expected_title!r} in the "
        f"downloads API response ({len(docs)} docs scanned). The CMS may "
        "not have published the issue yet."
    )


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------

# Tokens that bleed into the title band on the Gold ETF page only —
# ``NSE/ BSE Symbol: UNIONGOLD`` sits between the all-caps "GOLD ETF"
# title (top~76) and the next "(An open-ended..." line at top~104. We
# truncate the token list at the first such marker.
_TITLE_STOP_TOKENS = frozenset({"NSE/", "BSE", "Symbol:", "UNIONGOLD"})

# Accept all-caps token groups (Latin letters + Unicode curly apostrophes
# used in "CHILDREN'S"). Multi-line wrap is handled by combining all
# matching tokens within the top band, sorted by (top, x0).
_ALL_CAPS_RE = re.compile(r"[A-Z’']+$")


def _scheme_title_from_words(page) -> str | None:
    """Return the printed scheme name from the left-column title band, or
    ``None`` for non-scheme pages (cover / TOC / market review / NAV /
    SIP performance / disclosures).

    Strategy: extract words via ``page.extract_words(use_text_flow=True)``,
    filter to the title band (top < 100, x0 < 200), then take all-uppercase
    word tokens plus ``&`` in (top, x0) order and join them. The literal
    word ``Union`` is prepended (Union prints it in a separate visual band
    above the all-caps fund-name body — pdfplumber treats it as its own
    word with no consistent vertical relationship to the rest of the
    title, so we hard-code the brand prefix rather than recompose it).

    Returns ``None`` when no all-caps tokens are present in the band.
    """
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None
    band = [
        w for w in words
        if 0 < w["top"] < 100 and 0 < w["x0"] < 200
    ]
    band.sort(key=lambda w: (w["top"], w["x0"]))
    title_tokens: list[str] = []
    for w in band:
        t = w["text"].strip()
        if t in _TITLE_STOP_TOKENS:
            break
        if t == "&" or _ALL_CAPS_RE.match(t):
            title_tokens.append(t)
    if not title_tokens:
        return None
    # Drop trailing "&" that survived bullet/symbol cleanup on a
    # title-wrap edge case.
    while title_tokens and title_tokens[-1] == "&":
        title_tokens.pop()
    if not title_tokens:
        return None
    title_body = re.sub(r"\s+", " ", " ".join(title_tokens)).strip()
    return f"Union {title_body}"


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Union prints PTR as "Portfolio Turnover Ratio$$$ ... X.XX times". On
# most equity pages the label sits on its own line above a row of four
# Quantitative-Indicator values (``Std. Dev | Sharpe | Beta | Portfolio
# Turnover``), with the PTR being the LAST value in that row:
#
#     Std. Sharpe          Portfolio
#     Deviation Ratio Beta Turnover Ratio$$$
#     14.90%   0.58  0.95  0.77 times
#
# A handful of pages render it inline as ``Portfolio Turnover Ratio$$$ :
# 1.34 times`` (e.g. p8, p13, p17, p18, p20, p24, p29 in 2026-04). One
# regex captures both shapes: anchor on ``Turnover\s+Ratio``, then take
# the **last** ``X.XX times`` token in the next ~200 characters (the
# inline shape has the value adjacent to the label; the stacked shape
# puts three intermediate Std-Dev / Sharpe / Beta numeric tokens before
# the PTR value on the next line). DOTALL lets ``.`` span line breaks.
# We then pick the last match via ``findall``.
#
# Storage convention is a fraction (Union prints "times" — 0.77 = 77%,
# pass-through, no /100).
_PTR_RE = re.compile(
    r"Turnover\s+Ratio[^a-zA-Z%]*?(?:\d+\.\d+\s*%?\s*){0,3}(\d+\.\d+)\s*times",
    re.IGNORECASE | re.DOTALL,
)


def _extract_ptr(page) -> float | None:
    """Return the Portfolio Turnover Ratio in fraction form, or ``None``.

    Crops the page to the right-half left column (x = 380-580) where the
    "Quantitative Indicators - Growth Option" block lives, then runs the
    regex against the cropped text. Cropping avoids the heavy column-
    bleed between the central portfolio table and the right-side glossary
    that mangles the inline label on whole-page ``extract_text``.
    """
    try:
        crop = page.crop((380, 0, 580, page.height))
        text = crop.extract_text() or ""
    except Exception:  # noqa: BLE001
        return None
    m = _PTR_RE.search(text)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    if val != val or val <= 0:
        return None
    return val


@register_adapter
class UnionAdapter(ManagerAdapter):
    """Union Mutual Fund factsheet adapter.

    ``amc_slug = 'union'`` matches ``scheme_master.amc_code`` exactly
    (verified against the 32 active Union DIRECT/GROWTH schemes in the
    master table), so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "union"
    source_label = "Union Mutual Fund"

    # -------------------------------------------------------------------
    # URL
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return Union's downloads API endpoint URL.

        The per-month PDF URL on Union's CMS embeds a non-deterministic
        ``sfvrsn`` cache-buster query string and the filename slug is
        not stable across months, so we cannot construct the URL
        directly. ``build_url`` therefore returns the deterministic API
        endpoint and ``fetch`` resolves the per-month PDF URL via the
        API at download time.

        The ``ym`` argument is accepted but unused (the API is
        month-agnostic — it returns every factsheet entry in one shot).
        """
        return _API_URL

    def fetch(self, ym: str) -> Path:
        """Resolve the data-month PDF URL via the downloads API, then
        download to the canonical cache path.

        Re-uses an existing cached PDF unconditionally — delete the file
        to force a re-fetch. We override the base implementation because
        ``build_url`` returns the API endpoint (not a PDF URL), so the
        default ``fetch`` would download JSON. Two-step: API → resolved
        PDF URL → ``download_to``.
        """
        from mfs import paths
        from mfs.io.http import download_to

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        pdf_url = _resolve_factsheet_url(ym)
        return download_to(pdf_url, out)

    # -------------------------------------------------------------------
    # PTR / AUM
    # -------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                scheme = _scheme_title_from_words(page)
                if not scheme:
                    continue
                ptr = _extract_ptr(page)
                if ptr is None:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr,
                    source_amc=self.amc_slug,
                )

    # -------------------------------------------------------------------
    # Holdings — deferred. The factsheet portfolio table prints company
    # name + percent-to-NAV but no ISIN, so it cannot satisfy the
    # (scheme_code, isin, weight) contract. Phase 3.C's parallel
    # ISIN-tagged Excel path covers Union for the portfolio overlap
    # metric.
    # -------------------------------------------------------------------

    def parse_holdings(
        self, pdf_path: Path, ym: str
    ) -> Iterable[ParsedHoldingRecord]:
        return ()
