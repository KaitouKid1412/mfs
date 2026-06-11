"""Samco Mutual Fund monthly portfolio holdings adapter (Phase 5).

Samco publishes one SEBI-format Excel per scheme per month — the ideal shape
for ``GenericHoldingsAdapter`` + the shared ``parse_sebi_excel`` (sheet 0
carries the equity table in the canonical ISIN / Name / % to Net Assets
layout). Weights are stored as FRACTIONS (e.g. ``0.0675`` for a 6.75%
position); ``parse_sebi_excel`` auto-detects that and scales to percent, so we
inherit the parser wholesale.

Discovery is the only bespoke part — and it is pleasantly simple: the
statutory-disclosures page

    https://www.samcomf.com/StatutoryDisclosure

is server-rendered (despite the rest of samcomf.com being a SPA) and embeds
every document's absolute CDN URL directly in the HTML, so a single page
scrape gives us the whole catalogue. Monthly-portfolio files live on the CDN at:

    https://media1.samco.in/scomamc/amc_documents/
        IN_MF_MONTHLY_PORTFOLIO_<Month>[_]<YYYY>_<SchemeNameRaw>_<epoch>.xlsx

The filename encodes the DATA month-end (Samco publishes April data in May,
the usual +1 offset, but the *filename* carries the data month, so we key off
that and next month just works). Naming is inconsistent across the back
catalogue and we defend against all of it:

- Month token: usually the full English name (``April``); Samco has also
  shipped the misspelling ``Apirl`` for April 2026, so the month vocabulary
  includes that typo.
- The separator between month and year is sometimes ``_`` (``April_2026``)
  and sometimes absent (``August2025``) — the regex makes it optional.
- Scheme-name formatting is all over the place: underscore-separated words
  (``Samco_Small_Cap_Fund``), camel-glued (``SamcoLargeCapFund``,
  ``SamcoMid_CapFund``), or fully glued ALLCAPS (``SAMCOFLEXICAPFUND`` in some
  months). ``_clean_scheme_name`` un-underscores, splits camelCase, and
  title-cases ALLCAPS tokens into a readable printed name. The downstream
  fuzzy scheme matcher absorbs any residual cosmetic differences (e.g.
  "Flexicap" vs "Flexi Cap").

The page lists every financial year's monthly files plus fortnightly /
half-yearly (``IN_MF_FORTNIGHTLY_*`` / ``IN_MF_HY_PORTFOLIO_*``) and overnight
files; the regex is anchored to ``IN_MF_MONTHLY_PORTFOLIO_`` so only the
monthly per-scheme portfolios match, and we then filter to the requested data
month. Validated on April 2026: 12 schemes discovered; Samco Flexicap Fund
yields ~50 equity ISIN rows summing to ~85%.
"""

from __future__ import annotations

import re

from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import fetch_bytes
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = "https://www.samcomf.com/StatutoryDisclosure"

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
# Month token -> 1..12, including Samco's known "Apirl" misspelling of April.
_MONTH_TO_INT: dict[str, int] = {m.lower(): i + 1 for i, m in enumerate(_MONTHS)}
_MONTH_TO_INT["apirl"] = 4

# Alternation of all accepted month tokens, longest-first so e.g. "March"
# can't be shadowed by a shorter prefix.
_MONTH_ALT = "|".join(
    sorted([*_MONTHS, "Apirl"], key=len, reverse=True)
)

# Full monthly-portfolio CDN URL. Month token, year, scheme-name blob, and the
# epoch cache-buster are captured separately. The month/year separator is
# optional ("April_2026" and "August2025" both occur). The scheme blob is
# non-greedy up to the trailing "_<8+ digit epoch>.xlsx" so a scheme name that
# contains digits can't swallow the epoch.
_URL_RE = re.compile(
    r"(https://media1\.samco\.in/scomamc/amc_documents/"
    r"IN_MF_MONTHLY_PORTFOLIO_"
    r"(?P<month>" + _MONTH_ALT + r")_?(?P<year>\d{4})_"
    r"(?P<scheme>.+?)_(?P<epoch>\d{8,})\.xlsx)",
    re.IGNORECASE,
)


def _file_data_ym(month_token: str, year: str) -> str | None:
    """Map a filename's month token + year to data month YYYY-MM, or None."""
    month = _MONTH_TO_INT.get(month_token.lower())
    if month is None:
        return None
    return f"{int(year):04d}-{month:02d}"


# Glued compound words that the camelCase splitter cannot separate (no
# internal case boundary, e.g. "Flexicap"/"FLEXICAP"). The fuzzy matcher uses
# token_set_ratio, which treats the glued "FLEXICAP" as a single token and
# scores only 82 against scheme_master's "FLEXI CAP" (two tokens) — below the
# 85 acceptance threshold — even though plain Levenshtein ratio is 97. Splitting
# the token here makes the printed name word-segment-identical to the master so
# the match lands at 100. Applied case-insensitively per word.
_GLUED_WORD_SPLITS = {"flexicap": "Flexi Cap"}


def _clean_scheme_name(raw: str) -> str:
    """Turn the raw filename scheme blob into a readable printed name.

    Handles the three observed shapes: underscore-separated words, camelCase
    glued words, and ALLCAPS, plus glued compounds with no case boundary
    (``Flexicap`` -> ``Flexi Cap``) that the camelCase splitter can't reach.
    The downstream fuzzy matcher tolerates any residual cosmetic gap.
    """
    s = raw.replace("_", " ")
    # Split camelCase boundaries: lower|UPPER and UPPER|UPPER+lower.
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    words = []
    for w in s.split():
        split = _GLUED_WORD_SPLITS.get(w.lower())
        if split is not None:
            words.append(split)
        elif w.upper() == "ELSS":
            words.append("ELSS")
        elif w.isupper() and len(w) > 1:
            words.append(w.title())
        else:
            words.append(w)
    return re.sub(r"\s+", " ", " ".join(words)).strip()


@register_adapter
class SamcoHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "samco"
    source_label = "Samco Mutual Fund"
    # Per-scheme files put the portfolio on sheet 0 in the standard SEBI
    # layout (fraction units); parse_sebi_excel handles the rest.
    sheet_index = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {printed_scheme_name: absolute .xlsx URL} for data month
        ``ym`` (e.g. '2026-04').

        Scrapes the server-rendered disclosures HTML, keeps every
        monthly-portfolio CDN URL whose filename data-date falls in ``ym``,
        and maps each scheme's cleaned name to its .xlsx URL.
        """
        html = fetch_bytes(_DISCLOSURE_PAGE).decode("utf-8", errors="replace")
        out: dict[str, str] = {}
        seen_urls: set[str] = set()
        for m in _URL_RE.finditer(html):
            if _file_data_ym(m.group("month"), m.group("year")) != ym:
                continue
            url = m.group(1)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            name = _clean_scheme_name(m.group("scheme"))
            if not name:
                continue
            # First occurrence of a scheme name wins.
            out.setdefault(name, url)
        log.info("holdings.samco.discover", ym=ym, n_schemes=len(out))
        return out
