"""Capitalmind Mutual Fund monthly portfolio holdings adapter (Phase 5).

Capitalmind publishes ONE SEBI-format Excel per scheme per month — the ideal
shape for ``GenericHoldingsAdapter`` + the shared ``parse_sebi_excel``.

The statutory-disclosures page (https://capitalmindmf.com/statutory-disclosures
.html) is server-rendered HTML (a Strapi-backed marketing site): every
portfolio Excel link is embedded directly in the markup as a root-relative
``/uploads/<file>.xlsx`` href, so a single page scrape gives the whole catalog.
This is the HDFC/Helios discovery shape (links-in-HTML), so we reuse
``discover_xlsx_links`` and absolutize the root-relative URLs against the page
origin.

Filename naming is human-entered and HIGHLY inconsistent month to month — the
only stable signals are:
  - a scheme-code prefix on the filename
    (``CMFLEXI`` / ``CMLIQ`` / ``CMMAAF`` / ``CMARB``, occasionally just
     ``Capitalmind`` on the oldest Flexi-Cap files),
  - the word "Monthly" (vs "Fortnightly" / "Half_Yearly", which we exclude),
  - a month token (full name or 3-letter abbrev) + a 4-digit year.
Strapi appends an opaque hash to every filename and the AMC freely reorders /
re-words the middle ("Capitalmind", "Disclosure", "31st", "28", "Final",
"1_Updated", …), so we DON'T try to parse a scheme name out of the filename.
Instead we map the scheme-code prefix to the printed scheme name and key the
data month off the (month-token, year) pair. The publish label on the page is
the same calendar month as the data month, so no publish-month offset applies.

We deliberately keep ONLY ``...Monthly...`` portfolio files and skip the
Fortnightly / Half-Yearly portfolios, the AAUM / Commission / Investor-
Reporting (``IR_``) spreadsheets, and any non-portfolio file on the same page.

Excel layout (validated against Capitalmind Flexi Cap Fund, April 2026):
- Single sheet named ``<CODE>_<Month DD, YYYY>`` (e.g. 'CMFCF_April 30, 2026').
- Rows 3-5: "SCHEME NAME :", "INCEPTION DATE :", "MONTHLY PORTFOLIO STATEMENT
  AS ON : April 30, 2026" banners.
- Row 7: column header — Name of the Instrument / Issuer | ISIN |
  Rating / Industry^ | Quantity | Market value (Rs. in Lakhs) | % to NAV | YTM%.
- Row 8+: section banners ('Equity & Equity related', '(a) Listed / awaiting
  listing on Stock Exchanges', plus Debt / Money-Market / Cash banners on the
  hybrid + liquid schemes) interleaved with ISIN-bearing holding rows.

CRITICAL UNIT NOTE: Capitalmind stores '% to NAV' as a FRACTION (Federal Bank
at 0.0351 == 3.51%); the shared ``parse_sebi_excel`` auto-detects this (the
ISIN-weight total is ~1.0, not ~100) and scales to canonical percent — no
bespoke parser needed.
"""

from __future__ import annotations

import re

from mfs.ingest.holdings._generic import (
    GenericHoldingsAdapter,
    discover_xlsx_links,
)
from mfs.ingest.holdings._registry import register_adapter
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = "https://capitalmindmf.com/statutory-disclosures.html"
_ORIGIN = "https://capitalmindmf.com"

# Scheme-code prefix (filename head) -> printed scheme name. The printed name
# is what the fuzzy matcher compares against scheme_master (which canonicalizes
# 'CAPITALMIND FLEXI CAP FUND DIRECT GROWTH' -> 'CAPITALMIND FLEXI CAP FUND').
_PREFIX_TO_SCHEME: dict[str, str] = {
    "cmflexi": "Capitalmind Flexi Cap Fund",
    "cmliq": "Capitalmind Liquid Fund",
    "cmmaaf": "Capitalmind Multi Asset Allocation Fund",
    "cmarb": "Capitalmind Arbitrage Fund",
}

# The oldest Flexi-Cap files predate the CMFLEXI prefix and lead with a bare
# "Capitalmind_Monthly_Portfolio_Disclosure_<Mon>_<YYYY>" — those are Flexi Cap
# (it was the only equity scheme then). Matched only as a fallback so a generic
# "Capitalmind_..." name never shadows a code-prefixed one.
_BARE_CAPITALMIND_SCHEME = "Capitalmind Flexi Cap Fund"

_MONTH_TOKENS: dict[int, tuple[str, ...]] = {
    1: ("january", "jan"),
    2: ("february", "feb"),
    3: ("march", "mar"),
    4: ("april", "apr"),
    5: ("may",),
    6: ("june", "jun"),
    7: ("july", "jul"),
    8: ("august", "aug"),
    9: ("september", "sept", "sep"),
    10: ("october", "oct"),
    11: ("november", "nov"),
    12: ("december", "dec"),
}
_MONTH_TO_INT: dict[str, int] = {
    tok: m for m, toks in _MONTH_TOKENS.items() for tok in toks
}

# Any /uploads/*.xlsx URL on the page. We do the Monthly/scheme/month filtering
# in ``scheme_from_filename`` rather than the URL regex, because the filename
# middle is too inconsistent to anchor reliably in one pattern.
_URL_RE = re.compile(r'(?P<url>/uploads/[^"\']+?\.xlsx?)', re.IGNORECASE)

# A 4-digit year token in the filename.
_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _filename_data_ym(filename: str) -> str | None:
    """Reverse-derive data_ym ('YYYY-MM') from a Capitalmind monthly filename.

    Matches the first recognized month token paired with the first 4-digit
    year. Returns None if no month/year pair is found.
    """
    name = filename.lower()
    year_m = _YEAR_RE.search(name)
    if not year_m:
        return None
    year = int(year_m.group(1))
    # Find a month token as a whole word (underscore/space/digit delimited),
    # longest tokens first so 'sept'/'march' win over 'sep'/'mar'.
    for tok in sorted(_MONTH_TO_INT, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(tok)}(?![a-z])", name):
            return f"{year:04d}-{_MONTH_TO_INT[tok]:02d}"
    return None


def _scheme_from_filename(filename: str) -> str | None:
    """Turn a Capitalmind /uploads filename into the printed scheme name, or
    None to skip the file.

    Keeps ONLY monthly portfolio disclosures: the filename must contain
    'monthly' and 'portfolio' and NOT 'fortnightly' / 'half'. Maps the
    leading scheme-code prefix to the printed scheme name (falling back to the
    bare-'Capitalmind' equity files = Flexi Cap)."""
    name = filename.lower()
    if "monthly" not in name or "portfolio" not in name:
        return None
    if "fortnightly" in name or "half" in name:
        return None
    for prefix, scheme in _PREFIX_TO_SCHEME.items():
        if name.startswith(prefix):
            return scheme
    if name.startswith("capitalmind"):
        return _BARE_CAPITALMIND_SCHEME
    return None


@register_adapter
class CapitalmindHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "capitalmind"
    source_label = "Capitalmind Mutual Fund"
    # Per-scheme files put the portfolio on sheet 0 in the standard SEBI layout.
    sheet_index = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Scrape the disclosures HTML and keep only monthly-portfolio Excels
        whose filename month/year is the requested data month ``ym``.

        Root-relative ``/uploads/`` hrefs are absolutized against the page
        origin by ``discover_xlsx_links``. One file per scheme — first
        occurrence (the page lists newest-first) wins for the month."""

        def scheme_if_month_matches(filename: str) -> str | None:
            if _filename_data_ym(filename) != ym:
                return None
            return _scheme_from_filename(filename)

        out = discover_xlsx_links(
            _DISCLOSURE_PAGE,
            _URL_RE,
            scheme_if_month_matches,
            base_url=_ORIGIN,
        )
        log.info("holdings.capitalmind.discover", ym=ym, n_schemes=len(out))
        return out
