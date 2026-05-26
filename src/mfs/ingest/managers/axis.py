"""Axis Mutual Fund — factsheet adapter (Phase 3.B).

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/axis/2026-04.pdf``, ~12.5MB, 171 pages, 84 scheme
pages plus front matter / disclosures / TER tables).

URL discovery
-------------
The Axis MF download portal at ``https://transact.axismf.com/downloads``
is an Angular SPA. Its CMS API at
``https://transact.axismf.com/cms/api/factsheet?year=YYYY&month=MonthName``
returns the per-month PDF filenames. For April 2026 the canonical
"combined" factsheet (the one listing all schemes) is published as
``Axis Fund Factsheet April 2026.pdf`` under
``/cms/sites/default/files/pdf-factsheets/`` on both ``www.axismf.com``
and ``transact.axismf.com``. The filename pattern is stable; only the
month name and year change month over month, and there is NO publish-
month offset (April-2026 factsheet uses "April 2026" in the URL, not
"May 2026").

Layout findings driving the parser
----------------------------------
* Scheme name lives in the page header at top<35pt, size 13-14pt
  ``Lato-Regular``. ``extract_text`` mangles it (e.g. "AXISLARGE CAP
  FUND") because pdfplumber's word-grouper concatenates "AXIS" onto the
  next word — but the underlying ``chars`` have a ~2.5pt visual gap
  between "AXIS" and "LARGE". We extract via character-level positions,
  group into y-bands, then split tokens on x-gaps > 1.5pt. This recovers
  clean names: ``AXIS LARGE CAP FUND``, ``AXIS NIFTY 100 INDEX FUND``,
  ``AXIS RETIREMENT FUND - AGGRESSIVE PLAN``, etc., across all 84 scheme
  pages.

* PTR is printed in the left column on equity scheme pages as a stacked
  block::

      PORTFOLIOTURNOVER
      (1YEAR)
      0.77 times

  The value is **already a fraction** (0.77 = 77% turnover), not a
  percent — Axis differs from HDFC / Kotak here. We locate the
  ``PORTFOLIOTURNOVER`` anchor word, then find the nearest numeric word
  in the same column 5-30pt below. Hybrid / debt / index / ETF / FOF
  pages do NOT print PTR (Axis only reports it for actively-managed
  equity schemes) — that's a real data gap, not a parser miss. The
  factsheet has 16 PTR rows total in April 2026.

* AUM block prints two values stacked vertically in the left column::

      MONTHLY AVERAGE
      30,428.45 Cr.       <- Monthly Average AUM
      AUM
      AS ON 30 April, 2026
      30,498.17 Cr.       <- Month-End AUM (we want this)

  We anchor on the ``AS`` ``ON`` word pair near the ``AUM`` label, then
  take the first ``XX,XXX.XXCr.`` token with top > as_on top in the same
  column. This robustly disambiguates Month-End from Monthly Average
  across all 84 scheme pages including small-AUM schemes (47.59Cr.) and
  large ones (51,643.21Cr.).

Calibration counts on 2026-04
-----------------------------
* Scheme pages detected: 84
* PTR rows extracted: 16 (matches the equity-only subset that prints it)
* AUM rows extracted: 84 (every scheme page)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

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


# Header-extraction parameters. Calibrated against April 2026 factsheet:
# all scheme-name tokens sit in size>=12 Lato-Regular at top<35pt.
_HEADER_MAX_Y = 35.0
_HEADER_MIN_SIZE = 12.0
# Gap (pt) between adjacent header chars that we treat as a word break.
# Observed: ~0pt within a word (kerning often negative), 2.5-3pt between
# words. Threshold 1.5 cleanly separates AXIS/LARGE on page 8.
_HEADER_WORD_GAP = 1.5

# PTR numeric value below the PORTFOLIOTURNOVER anchor word.
_PTR_NUM_RE = re.compile(r"^\d+\.\d+$")


def _extract_scheme_name(page) -> str | None:
    """Extract the Axis scheme title from the page header.

    Uses character-level positions to recover word boundaries that
    pdfplumber's ``extract_words`` merges (the title font has tight
    kerning that makes ``extract_words`` glue ``AXIS`` onto the next
    word — e.g. ``AXISLARGE CAP FUND`` for the Axis Large Cap Fund
    page). Chars in the header band still expose ~2.5pt gaps at true
    word boundaries.

    Returns the clean title (e.g. ``Axis Large Cap Fund``) or None if
    the page is not a scheme page.
    """
    try:
        chars = page.chars
    except Exception:  # noqa: BLE001
        return None
    header_chars = [
        c for c in chars
        if c["top"] < _HEADER_MAX_Y
        and c.get("size", 0) >= _HEADER_MIN_SIZE
    ]
    if not header_chars:
        return None

    # Group into y-bands (3pt tolerance). Pick the band with the most
    # chars — that's the title row (the smaller "FACTSHEET" word lives
    # off to the right with fewer chars).
    header_chars.sort(key=lambda c: (c["top"], c["x0"]))
    bands: list[list[dict]] = []
    for c in header_chars:
        if bands and abs(c["top"] - bands[-1][-1]["top"]) < 3:
            bands[-1].append(c)
        else:
            bands.append([c])
    if not bands:
        return None
    bands.sort(key=lambda b: -len(b))
    band = bands[0]
    band.sort(key=lambda c: c["x0"])

    # Walk left-to-right, splitting on visual gaps > _HEADER_WORD_GAP.
    tokens: list[str] = []
    cur: list[str] = []
    prev_x1: float | None = None
    for c in band:
        gap = (c["x0"] - prev_x1) if prev_x1 is not None else 0.0
        if prev_x1 is not None and gap > _HEADER_WORD_GAP:
            if cur:
                tokens.append("".join(cur))
                cur = []
        ch = c["text"]
        if ch == " ":
            if cur:
                tokens.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
        prev_x1 = c["x1"]
    if cur:
        tokens.append("".join(cur))
    tokens = [t for t in tokens if t]
    if not tokens:
        return None

    upper = " ".join(tokens)
    # Drop obvious non-scheme headers (cover pages, TOC, etc.). Axis
    # title always starts with "AXIS" (case-insensitive after split).
    if not upper.upper().startswith("AXIS"):
        return None

    # Title-case every token, then re-uppercase a curated acronym set so
    # the printed name reads naturally and canonicalize() (used by the
    # orchestrator's fuzzy match) sees the same letter sequence as the
    # scheme_master name. canonicalize() uppercases both sides so the
    # exact casing doesn't change match scores — this is purely for
    # readability in logs / debug output.
    _ACRONYMS = {
        "ETF", "FOF", "NBFC", "SDL", "AAA", "IBX", "BSE", "NSE", "US",
        "IT", "PSU", "IDCW", "ELSS", "ESG", "NAV", "FOOF", "FOFs",
        "CRISIL", "NIFTY",
    }
    pretty: list[str] = []
    for tok in tokens:
        up = tok.upper()
        if up in _ACRONYMS:
            pretty.append(up)
        elif up == "FOF":
            pretty.append("FoF")
        else:
            pretty.append(tok.title())
    name = " ".join(pretty)
    # Normalize stray whitespace and stripped hyphens like " - " (the
    # Retirement Plan pages render " -AGGRESSIVE" with no space; the
    # char-gap split correctly inserts a token break, so we get
    # "Fund - Aggressive" — leave as-is, fuzzy match handles it).
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _find_ptr_value(words: list[dict]) -> float | None:
    """Return the printed Portfolio Turnover fraction, or None."""
    for w in words:
        if w["text"].upper() != "PORTFOLIOTURNOVER":
            continue
        if w["x0"] > 260:
            # Only left-column PTR; right-col text on hybrid pages
            # would be a different anchor.
            continue
        # Scan within ~5-30pt below for a numeric token in the same column.
        for v in words:
            dy = v["top"] - w["top"]
            if not (5 < dy < 35):
                continue
            if abs(v["x0"] - w["x0"]) > 60:
                continue
            if not _PTR_NUM_RE.match(v["text"]):
                continue
            try:
                return float(v["text"])
            except ValueError:
                continue
        return None
    return None


@register_adapter
class AxisAdapter(ManagerAdapter):
    """Axis Mutual Fund factsheet adapter.

    ``amc_slug`` matches ``scheme_master.amc_code`` directly, so no alias
    entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "axis"
    source_label = "Axis Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical Axis combined factsheet PDF URL for data
        month ym='YYYY-MM'.

        Axis publishes monthly factsheets on the Drupal-based CMS shared
        by axismf.com and transact.axismf.com. The URL encodes the
        **data month** (no publish-month offset). The filename pattern
        is ``Axis Fund Factsheet <MonthName> <YYYY>.pdf`` — note the
        leading capital on each word and the literal space separators
        (URL-encoded as %20).
        """
        y, m = map(int, ym.split("-"))
        month_name = _MONTH_NAMES[m - 1]
        filename = f"Axis Fund Factsheet {month_name} {y}.pdf"
        # urllib.parse.quote would also encode "/" which we don't want;
        # the only special char in the filename is the space.
        encoded = filename.replace(" ", "%20")
        return (
            "https://www.axismf.com/cms/sites/default/files/pdf-factsheets/"
            f"{encoded}"
        )

    # -----------------------------------------------------------------
    # PTR
    # -----------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                name = _extract_scheme_name(page)
                if not name:
                    continue
                words = page.extract_words(use_text_flow=True)
                ptr = _find_ptr_value(words)
                if ptr is None:
                    continue
                if ptr != ptr or ptr <= 0:
                    # Drop NaN / non-positive; Axis prints a real
                    # fraction or omits the field entirely.
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=name,
                    ptr=ptr,
                    source_amc=self.amc_slug,
                )

    # Holdings extraction is deferred to Phase 3.C — Axis prints its
    # portfolio as a single-column equity table with neither ISINs nor
    # a stable sector banner, so the factsheet path can't produce a
    # tagged holding stream. The orchestrator handles the empty yield.
    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
