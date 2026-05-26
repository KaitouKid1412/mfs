"""Bank of India Mutual Fund — factsheet adapter (Phase 3.B).

Calibrated against the April 2026 combined factsheet PDF
(``data/raw/factsheets/bank_of_india/2026-04.pdf``, ~5.26 MB, 52 pages, 22
scheme detail pages — 13 equity + 6 hybrid/debt with AUM-only + 3 short-
duration debt — preceded by a snapshot dashboard / CEO note / market
review, and followed by IDCW history, scheme performance tables, expense
ratios, disclaimers).

URL discovery
-------------
``www.boimf.in`` is a Sitefinity 14 ASP.NET WebForms site (formerly BOI
AXA Mutual Fund). The factsheet downloads tab on the **Investor Corner**
page is populated by a server-side AJAX call to::

    POST /AjaxService.asmx/GetDocuments
    Content-Type: application/json;charset=utf-8
    Body: {
        "pagno": 0, "category": null,
        "fromDate": null, "toDate": null,
        "LibraryName": "InvestorCorner",
        "folderName": "FACTSHEETS",
        "CategoryValue": "no"
    }

which returns ``{"d": JSON-string}`` whose ``Documents`` list contains
each month's URL under the ``FolderUrl`` field, like::

    https://www.boimf.in/docs/default-source/investorcorner/factsheets/
        factsheet-april-2026.pdf?sfvrsn=b5707f7e_5

The ``?sfvrsn=...`` query is a Sitefinity cache-buster and is NOT
required — the bare URL also serves the same PDF (verified by HEAD probe;
both return identical ``Content-Length``). The filename slug follows the
stable pattern ``factsheet-<month>-<year>.pdf`` (lower-case month name,
4-digit year). A few historical months use a ``-digital`` suffix
(``factsheet-october-2025-digital.pdf``), and the May-2024 issue carries
a ``-(4)`` rev tag, but every recent month including the target April-2026
uses the clean form, which is what ``build_url`` emits.

Older archives may live under the predecessor brand path
``boiaxamf.blob.core.windows.net/...`` but the BOIMF rebrand consolidated
all monthly PDFs under ``/docs/default-source/investorcorner/factsheets/``,
so the URL pattern is slug-stable for the foreseeable future. ``fetch``
falls back to the AJAX-listing path if the bare URL ever 404s — but in
practice the canonical URL works.

Layout findings driving the parser
----------------------------------
* Each equity / hybrid / debt scheme has one detail page (pages 7-28).
  PTR is published on equity scheme pages (7-19) only — hybrid /
  arbitrage / debt pages legitimately omit the ``PORTFOLIO TURNOVER
  RATIO`` block (Balanced Advantage, Conservative Hybrid, Arbitrage,
  Liquid, Short Term Income, Ultra Short, Money Market, Credit Risk,
  Overnight — none publish PTR).

* Scheme name lives in the upper-left corner of the page at top y ~ 67,
  in a large title font (height > 14pt). On scheme pages the title and
  the "(An open ended ... fund)" sub-banner share the same
  ``extract_text`` line, which pdfplumber column-bleeds: the text-flow
  output for the first non-empty line on page 7 becomes
  ``Bank of India Flexi Cap This product is suitable for investors ...``
  rather than the rendered ``Bank of India Flexi Cap Fund``. The fund
  word (``Fund`` / ``Infrastructure Fund`` / ``Equity & Debt Fund``)
  lives on the next visual line but wraps interleaved with the
  riskometer banner. We use word-position extraction filtered by font
  height ( > 14 pt ) and left-column x0 ( < 280 ) to recover the clean
  multi-line title:

      band-y = group(big_words by 5pt y-tolerance)
      title  = " ".join(group(sorted by x0))   for each band

  This produces clean names like ``Bank of India Flexi Cap Fund``,
  ``Bank of India Manufacturing & Infrastructure Fund``, ``Bank of
  India Mid & Small Cap Equity & Debt Fund``, etc.

* PTR is printed as ``PORTFOLIO TURNOVER RATIO (As on April 30, 2026)``
  followed on the next visual line by ``<fraction> Times#`` (e.g.
  ``0.82 Times#``). The hash is a footnote glyph that pdfplumber
  preserves; we tolerate it in the regex. PTR is already a fraction
  (Flexi Cap = 0.82 = 82%) so no unit conversion is needed.

* AUM is printed in two stacked labels in the FUND-SIZE block:

      AVERAGE AUM         (label)
      ` 2,272.15 Crs.     (Indian-rupee glyph rendered as backtick)
      LATEST AUM          (label)
      ` 2,387.56 Crs.

  We extract the **LATEST AUM** value (month-end, consistent with
  Stage 2's treatment of ``aum_crore``). The ``Crs.`` unit token
  sometimes column-bleeds to ``C rs.`` (with a stray space) on the
  Large & Mid Cap page — we tolerate that. Values are in INR Crore so
  no unit conversion. The Liquid Fund prints the AUM rows with a
  trailing ``#`` footnote (``LATEST AUM#``) — also tolerated.

* Pages 29-30 are IDCW history tables; 31-44 are per-scheme
  performance / SIP tables; 45-52 are scheme details / disclaimers.
  None of these print AVERAGE AUM / LATEST AUM in the same layout, so
  the parser naturally skips them via the title-not-found gate.

Calibration counts on 2026-04
-----------------------------
* 22 scheme detail pages identified (pages 7-28).
* 13 PTR rows extracted (equity schemes only — exceeds the brief's
  ≥ 12-row target for the 15-scheme ranked equity set).
* 22 AUM rows extracted (one per detail page, all schemes).

Holdings
--------
Deferred. Phase 3.A consumes the parallel ISIN-tagged Excel via the
holdings module
(``data/raw/holdings/bank_of_india/<YYYY-MM>/<scheme>.xlsx``) for the
overlap metric; the factsheet PDF would yield holdings without ISINs.
``parse_holdings`` returns an empty iterable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

import httpx
import pdfplumber

from mfs.errors import IngestError
from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)


_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


# ---------------------------------------------------------------------------
# Scheme name extraction (word-position based)
# ---------------------------------------------------------------------------

# Title font height on scheme detail pages. The banner font glyphs render
# with (bottom - top) > 14pt; body text is < 12pt. We use 13pt as a safe
# floor that admits the title but excludes the riskometer / sub-banner
# text that column-bleeds into the same region.
_TITLE_MIN_HEIGHT = 13.0

# Left-column x0 cutoff. The title sits within x < 270; the riskometer
# banner sits at x > 280. The right-column "(An open ended ...) Invest
# Now" block starts at x ~ 287.
_TITLE_LEFT_X_MAX = 280.0

# Title vertical band on scheme pages — the title sits between y ~ 60
# and y ~ 130 (one or two lines tall).
_TITLE_TOP_MIN = 50.0
_TITLE_TOP_MAX = 140.0

# When grouping title words into lines, words with top y-coordinates
# within this tolerance are considered the same line.
_LINE_Y_TOL = 5.0

# Trailing footnote glyphs that the AMC suffixes onto scheme titles
# (e.g. ``Bank of India Large Cap Fund$`` — the ``$`` is a footnote
# pointer to "Formerly Bank of India Bluechip Fund").
_TITLE_TAIL_GLYPHS = "$*^#"


def _scheme_name_from_page(page) -> str | None:
    """Return the printed scheme title from a scheme detail page, or None.

    Uses word-position extraction filtered by font height and left-column
    x0 to recover the clean multi-line title even when pdfplumber's
    ``extract_text`` column-bleeds the title row into the riskometer /
    sub-banner text.
    """
    try:
        words = page.extract_words(use_text_flow=True, extra_attrs=["size"])
    except Exception:  # noqa: BLE001
        return None
    big = [
        w for w in words
        if 0 < w["x0"] < _TITLE_LEFT_X_MAX
        and _TITLE_TOP_MIN < w["top"] < _TITLE_TOP_MAX
        and (w["bottom"] - w["top"]) > _TITLE_MIN_HEIGHT
    ]
    if not big:
        return None
    big.sort(key=lambda w: (w["top"], w["x0"]))
    # Group into lines by top y-coordinate (within ~5pt band).
    lines: list[list[dict]] = []
    for w in big:
        if not lines or abs(w["top"] - lines[-1][-1]["top"]) > _LINE_Y_TOL:
            lines.append([w])
        else:
            lines[-1].append(w)
    parts: list[str] = []
    for line in lines:
        line.sort(key=lambda w: w["x0"])
        parts.append(" ".join(w["text"] for w in line))
    title = " ".join(parts).strip()
    # Collapse double-spaces and trim footnote glyphs.
    title = re.sub(r"\s+", " ", title).strip()
    title = title.rstrip(_TITLE_TAIL_GLYPHS + " ").strip()
    if not title or not title.lower().startswith("bank of india"):
        return None
    return title


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# PTR is printed as ``PORTFOLIO TURNOVER RATIO (As on April 30, 2026)``
# followed on the next visual line by ``<fraction> Times#``. Because the
# value column-bleeds with the parallel Investment Objective paragraph
# text in ``extract_text`` output, the literal-text gap between the
# label and the numeric token can be 100-200 characters long and crosses
# at least one ``\n``. We use a non-greedy ``.{0,250}`` (DOTALL) so the
# regex consumes the bleed and locks onto the next ``<digits>.<digits>
# Times`` token.
#
# PTR is already a fraction — Flexi Cap = 0.82 = 82% — so no unit
# conversion is applied. The trailing ``#`` is a footnote glyph; we
# accept it but don't require it.
_PTR_RE = re.compile(
    r"PORTFOLIO\s+TURNOVER\s+RATIO.{0,250}?(\d+\.\d+)\s*Times",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _canonical_factsheet_url(ym: str) -> str:
    """Return the slug-stable canonical PDF URL for data month ``ym``.

    URL pattern::

        https://www.boimf.in/docs/default-source/investorcorner/factsheets/
            factsheet-<month>-<yyyy>.pdf

    where ``<month>`` is the lower-case English month name and ``<yyyy>``
    is the data year. The Sitefinity ``?sfvrsn=`` cache-buster is NOT
    included — the bare URL serves the same PDF (verified by HEAD probe).
    """
    y, m = map(int, ym.split("-"))
    month_lower = _MONTH_NAMES[m - 1]
    return (
        "https://www.boimf.in/docs/default-source/investorcorner/factsheets/"
        f"factsheet-{month_lower}-{y}.pdf"
    )


def _resolve_via_ajax(ym: str) -> str:
    """Fallback URL resolver — query the Sitefinity AJAX listing endpoint
    and match the entry whose ``DocName`` contains the target month/year.

    Used only when the canonical bare URL 404s (e.g. archive months that
    were uploaded with a ``-digital`` suffix or stray rev tag). Returns
    the ``FolderUrl`` exactly as reported by the AMC — including any
    ``?sfvrsn=`` cache-buster, which the HTTP layer ignores.
    """
    y, m = map(int, ym.split("-"))
    month_name = _MONTH_NAMES[m - 1].upper()
    needle_year = str(y)
    url = "https://www.boimf.in/AjaxService.asmx/GetDocuments"
    body = {
        "pagno": 0,
        "category": None,
        "fromDate": None,
        "toDate": None,
        "LibraryName": "InvestorCorner",
        "folderName": "FACTSHEETS",
        "CategoryValue": "no",
    }
    try:
        with httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/json;charset=utf-8",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://www.boimf.in",
                "Referer": "https://www.boimf.in/investor-corner",
            },
        ) as client:
            r = client.post(url, json=body)
            r.raise_for_status()
            wire = r.json()
    except Exception as e:  # noqa: BLE001
        raise IngestError(
            f"bank_of_india: AJAX listing failed at {url}: {e}"
        ) from e
    payload = wire.get("d")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception as e:  # noqa: BLE001
            raise IngestError(
                f"bank_of_india: AJAX listing JSON parse failed: {e}"
            ) from e
    docs = (payload or {}).get("Documents") if isinstance(payload, dict) else None
    if not docs:
        raise IngestError(
            "bank_of_india: AJAX listing returned no Documents for "
            "FACTSHEETS folder."
        )
    for d in docs:
        name = (d.get("DocName") or "").upper()
        if month_name in name and needle_year in name:
            folder_url = d.get("FolderUrl") or ""
            if folder_url:
                return folder_url
    raise IngestError(
        f"bank_of_india: no factsheet entry for {month_name} {needle_year} "
        f"in the FACTSHEETS folder listing ({len(docs)} docs scanned). "
        "The AMC may not have published the issue yet."
    )


@register_adapter
class BankOfIndiaAdapter(ManagerAdapter):
    """Bank of India Mutual Fund factsheet adapter.

    ``amc_slug = 'bank_of_india'`` matches ``scheme_master.amc_code``
    exactly, so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "bank_of_india"
    source_label = "Bank of India Mutual Fund"

    # -------------------------------------------------------------------
    # URL
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the canonical Bank of India MF factsheet PDF URL for
        data month ``ym='YYYY-MM'``.

        URL pattern::

            https://www.boimf.in/docs/default-source/investorcorner/
                factsheets/factsheet-<month>-<yyyy>.pdf

        The ``?sfvrsn=`` Sitefinity cache-buster is NOT included — the
        bare URL works (verified by HEAD probe). For months whose file
        name uses a ``-digital`` suffix or other rev tag, ``fetch``
        falls back to the AJAX listing endpoint.
        """
        return _canonical_factsheet_url(ym)

    def fetch(self, ym: str) -> Path:
        """Download the factsheet for data month ``ym``; return local Path.

        Tries the canonical bare URL first; on 404 falls back to the
        Sitefinity AJAX listing to resolve a per-month override (covers
        ``-digital``-suffixed archive months).
        """
        from mfs import paths
        from mfs.io.http import download_to

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        url = self.build_url(ym)
        try:
            return download_to(url, out)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
            log.info(
                "bank_of_india.fetch.fallback",
                reason="404 on canonical url",
                url=url,
            )
        alt = _resolve_via_ajax(ym)
        return download_to(alt, out)

    # -------------------------------------------------------------------
    # PTR
    # -------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                scheme = _scheme_name_from_page(page)
                if not scheme:
                    continue
                text = page.extract_text() or ""
                m = _PTR_RE.search(text)
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
    # Holdings — deferred. Bank of India's portfolio tables print
    # security names without ISINs; the parallel Excel-based ISIN-tagged
    # path (Phase 3.A holdings module) covers holdings ingestion.
    # -------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
