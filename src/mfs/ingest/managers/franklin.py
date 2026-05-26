"""Franklin Templeton Mutual Fund (India) — factsheet adapter (Phase 3).

Calibrated against the April 2026 combined factsheet at
``data/raw/factsheets/franklin/2026-04.pdf`` (7.68 MB, 88 pages, 36 scheme
pages followed by SIP performance summaries / disclosures / IDCW history).

URL discovery
-------------
Franklin Templeton India's site at ``www.franklintempletonindia.com`` is an
Angular SPA. Factsheet PDFs are hosted on a Widen DAM
(``franklintempletonprod.widen.net``) and served through a domain proxy at
``/download/en-in/fund-factsheets/<documentId>/<filename>``. The
``<documentId>`` is a per-upload UUID, so the URL is NOT deterministic
month over month and must be resolved through the SPA's literature API:

    POST https://www.franklintempletonindia.com/api/literature/v1/documents
         Content-Type: application/json
         Body: {"channel": "en-in", "type": "FUND-FACTSHEETS"}

The response is a single JSON object ``{"document":[...]}`` listing every
piece of literature, with ``dctermsTitle`` (e.g. ``"Factsheet as on April
30, 2026"``), ``contentGrouping`` (``"FUND-FACTSHEETS"`` for the combined
monthly factsheet), ``documentId``, and ``literatureHref`` fields. We
filter for ``contentGrouping == "FUND-FACTSHEETS"`` and a title containing
the target month/year, then build the canonical proxy URL
``/download/en-in/fund-factsheets/<id>/<filename>``.

The literature payload also contains other document grouping (SIP
calculators, KIM, SID, etc), so the contentGrouping filter is essential —
the title-alone match would otherwise hit the SEBI-filing PDF copy.

``build_url(ym)`` returns the API endpoint (deterministic and stable);
``fetch(ym)`` overrides the default download to do the lookup-then-fetch
dance.

Layout findings driving the parser
----------------------------------
* Each scheme page starts with the scheme name on line 1, e.g.
  ``Franklin India Multi-Factor Fund FIMF`` (trailing token is the
  in-house scheme abbreviation — ``FIMF``, ``FILCF``, etc) optionally
  followed by ``$$`` footnote markers or wrapped lines (Money Market Fund
  on p41 wraps as ``Franklin India Money Market Fund`` / ``FIMMF``).
  Pages 40 (Liquid), 41 (Money Market), 44 (Banking & PSU Debt) put a
  CRISIL/ICRA rating banner on lines 1-2 and the scheme name on line 3.
  We scan the first 6 lines for a line that starts with one of the known
  Franklin/Templeton prefixes AND contains the word ``Fund``. The trailing
  scheme abbreviation, ``$$`` markers, and any ``(CODE)`` parenthetical
  are stripped so the printed name matches scheme_master.

* PTR is printed in the FUND FEATURES block in the left column as a
  percent value:
      ``TURNOVER``
      ``Portfolio Turnover 26.58%``
  We store as a fraction (26.58% -> 0.2658). On the Arbitrage Fund page
  there are TWO PTR rows — ``Total Portfolio Turnover 959.18%`` and
  ``Portfolio Turnover (Equity) 1273.27%``. We prefer the ``(Equity)``
  variant when present (representative of stock-picking churn rather
  than derivative roll); fall back to the plain ``Portfolio Turnover``
  number otherwise.

* Pages 56-79 are SIP performance summary pages whose first line is a
  scheme name suffixed with ``(CODE) - Regular Growth Option`` etc.
  These pages do NOT carry Portfolio Turnover blocks, so the parser
  naturally produces no rows for them (the marker gate filters them
  out). The same is true for the snapshot pages 5-11 which would
  otherwise match the prefix-only check.

Calibration counts on 2026-04
-----------------------------
* 36 scheme pages detected (the equity/hybrid/debt/index universe)
* 22 PTR rows extracted (equity + a few hybrids + arbitrage; pure debt /
  index / FoF pages don't print Portfolio Turnover)
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
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# Scheme-line prefixes Franklin Templeton uses on factsheet pages. The
# India entity brands its schemes "Franklin India X Fund", but legacy
# Templeton-branded schemes still carry "Templeton India" and a handful
# of overseas-FoF / cross-border schemes use "Franklin Asian", "Franklin
# Build", or "Franklin U.S.".
_SCHEME_PREFIXES = (
    "Franklin India ",
    "Templeton India ",
    "Franklin Asian ",
    "Franklin Build ",
    "Franklin U.S. ",
    "Franklin US ",
)

# Fund-word anchor — required to disambiguate real scheme pages from
# multi-scheme snapshot banners (pp 5/9/10/11) whose first line is a
# wrapped scheme-list fragment that happens to start with a prefix.
_FUND_WORD_RE = re.compile(r"\b(?:Fund|FoF|ETF)\b", re.IGNORECASE)

# Trailing junk patterns on the scheme line:
#  - " - Regular Growth Option" / " - Direct Growth" (SIP perf banners)
#  - " (CODE)" parenthetical scheme abbreviation
#  - One or more trailing "$" footnote glyphs
#  - One trailing whitespace-separated all-caps scheme code (e.g. "FIMF")
_PLAN_SUFFIX_RE = re.compile(r"\s*[-–—]\s*(?:Regular|Direct).*$", re.IGNORECASE)
_CODE_PAREN_RE = re.compile(r"\s*\([A-Z]{2,8}\)\s*$")
_DOLLAR_TAIL_RE = re.compile(r"[\$\*\^]+\s*$")
_TRAIL_ABBREV_RE = re.compile(r"\s+[A-Z]{3,8}\s*$")

# Text-extract PTR regex — picks up clean rows like
#   "Portfolio Turnover 26.58%"
#   "Portfolio Turnover (Equity) ** 1273.27%"
#   "Total Portfolio Turnover $ 959.18%"
# Footnote glyphs ($, *, ^, #, @) and whitespace are tolerated between
# the label and the numeric value. The optional "(Equity)" capture lets
# the caller prefer the equity variant on arbitrage / hybrid pages where
# both a "Total" and an "(Equity)" row appear.
_PTR_RE = re.compile(
    r"Portfolio\s+Turnover\s*(\(Equity\))?[\s\$\*\^\#@]*(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

def _publish_ym(data_ym: str) -> tuple[int, int]:
    """Publish month = data month + 1 (with year rollover)."""
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return y + 1, 1
    return y, m + 1


def _clean_scheme_name(raw: str) -> str | None:
    """Strip plan-suffix, code parens, dollar/star/caret tails, and the
    trailing all-caps scheme abbreviation. Returns None if cleanup leaves
    nothing recognisable.
    """
    s = raw.strip()
    # If a second prefix appears later in the line, it's a multi-scheme
    # banner — keep only the portion up to the second occurrence.
    for pref in _SCHEME_PREFIXES:
        idx = s.find(pref, 1)  # search past the leading match
        if idx > 0:
            s = s[:idx].strip()
            break
    s = _PLAN_SUFFIX_RE.sub("", s).strip()
    s = _CODE_PAREN_RE.sub("", s).strip()
    s = _DOLLAR_TAIL_RE.sub("", s).strip()
    s = _TRAIL_ABBREV_RE.sub("", s).strip()
    # Re-strip dollar tails uncovered after stripping the abbreviation
    # (the order ".Fund$$ FIMF" vs ".Fund FIMF$$" varies by page).
    s = _DOLLAR_TAIL_RE.sub("", s).strip()
    if not s:
        return None
    return s


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme title from a scheme page, or None.

    Scans the first 6 non-empty lines for a line that (a) starts with one
    of the Franklin/Templeton brand prefixes and (b) contains the word
    ``Fund`` / ``FoF`` / ``ETF``. The result is canonicalised by stripping
    trailing footnote glyphs, the in-house scheme-code abbreviation, and
    any parenthetical CODE / Regular-Growth-Option SIP-table suffix.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:6]
    for ln in lines:
        if not any(ln.startswith(p) for p in _SCHEME_PREFIXES):
            continue
        if not _FUND_WORD_RE.search(ln):
            continue
        cleaned = _clean_scheme_name(ln)
        if cleaned:
            return cleaned
    return None


def _extract_ptr(text: str) -> float | None:
    """Return Franklin PTR as a fraction (storage convention) or None.

    Franklin prints PTR as a percent (e.g. ``26.58%``); we divide by 100.
    On arbitrage pages there are two rows — prefer the ``(Equity)``
    variant when present (representative of stock-picking churn rather
    than derivative roll).
    """
    matches = list(_PTR_RE.finditer(text))
    if not matches:
        return None
    chosen = None
    for m in matches:
        if m.group(1):  # "(Equity)" tag
            chosen = m
            break
    if chosen is None:
        chosen = matches[0]
    try:
        pct = float(chosen.group(2))
    except ValueError:
        return None
    return pct / 100.0


def _resolve_factsheet_url(ym: str) -> str:
    """Query the Franklin Templeton India literature API for the data-month
    factsheet PDF URL.

    Posts to ``/api/literature/v1/documents`` with the FUND-FACTSHEETS
    type and filters the (large) response for the entry whose
    ``contentGrouping == 'FUND-FACTSHEETS'`` and whose
    ``dctermsTitle`` matches the target month/year. Returns the
    canonical proxy download URL.

    Raises IngestError if the API returns no match — we never fall back
    to a hand-rolled URL guess (the UUID is non-deterministic).
    """
    import httpx

    y, m = map(int, ym.split("-"))
    month_name = _MONTH_NAMES[m - 1]
    api_url = (
        "https://www.franklintempletonindia.com/api/literature/v1/documents"
    )
    payload = {"channel": "en-in", "type": "FUND-FACTSHEETS"}
    try:
        with httpx.Client(
            timeout=60.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            follow_redirects=True,
        ) as c:
            r = c.post(api_url, json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        raise IngestError(
            f"franklin: literature API failed at {api_url}: {e}"
        ) from e
    docs = data.get("document") or []
    needle_month = month_name.lower()
    needle_year = str(y)
    for d in docs:
        if (d.get("contentGrouping") or "").upper() != "FUND-FACTSHEETS":
            continue
        title = (d.get("dctermsTitle") or "").lower()
        if needle_month not in title or needle_year not in title:
            continue
        href = d.get("literatureHref") or ""
        if not href:
            continue
        # literatureHref looks like
        # "/en-in/fund-factsheets/<uuid>/Factsheet-as-on-April-30-2026_web.pdf"
        if href.startswith("/"):
            return f"https://www.franklintempletonindia.com/download{href}"
        return href
    raise IngestError(
        f"franklin: no FUND-FACTSHEETS document for {month_name} {y} in the "
        f"literature API response ({len(docs)} docs scanned). The CMS may "
        "not have published the issue yet."
    )


@register_adapter
class FranklinAdapter(ManagerAdapter):
    """Franklin Templeton India factsheet adapter.

    ``amc_slug = 'franklin'`` does NOT match ``scheme_master.amc_code``
    (which is ``franklin_templeton``). The alias entry in
    ``_scheme_match._ADAPTER_SLUG_TO_SCHEME_MASTER_AMC_CODE`` bridges that
    gap — without it, the fuzzy-match would silently match zero schemes.
    """

    amc_slug = "franklin"
    source_label = "Franklin Templeton Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical Franklin combined-factsheet PDF URL for
        data month ym='YYYY-MM'.

        Franklin's PDFs are Widen-DAM hosted with a non-deterministic
        per-upload UUID, so the URL must be resolved via the literature
        API. ``build_url`` performs that resolution and returns the
        stable proxy download URL ``/download/en-in/fund-factsheets/
        <uuid>/Factsheet-as-on-<MonthName>-<DD>-<YYYY>_web.pdf``.

        The PDF is published mid-month following the data month (April
        data is uploaded around May 13); calling before the upload date
        will raise ``IngestError``.
        """
        return _resolve_factsheet_url(ym)

    def fetch(self, ym: str) -> Path:
        """Resolve the data-month factsheet URL via the literature API,
        then download to the canonical cache path.

        Re-uses an existing cached PDF unconditionally — delete the file
        to force a re-fetch. We override the base implementation because
        ``build_url`` already triggers a network call; cheap-out of doing
        it twice when the cache hits.
        """
        from mfs import paths
        from mfs.io.http import download_to

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        url = self.build_url(ym)
        return download_to(url, out)

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
                ptr = _extract_ptr(text)
                if ptr is None:
                    continue
                # Fail-fast: NaN / non-positive → drop.
                if ptr != ptr or ptr <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr,
                    source_amc=self.amc_slug,
                )

    # ------------------------------------------------------------------
    # Holdings — deferred. Franklin's two-column portfolio layout has the
    # same column-bleed issues as HDFC/Kotak/Bandhan and no ISIN column.
    # Phase 3.C provides the parallel Excel-based ISIN-tagged path.
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
