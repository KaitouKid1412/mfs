"""Union Mutual Fund monthly portfolio holdings adapter (Phase 5).

Union publishes ONE SEBI-format ``.xlsx`` per scheme per month — the ideal
shape for ``GenericHoldingsAdapter`` + the shared ``parse_sebi_excel``.

Discovery (static HTML, HDFC-style)
-----------------------------------
The "Monthly Portfolio" page
(https://www.unionmf.com/about-us/downloads/monthly-portfolio) is a
Sitefinity site, but — like HDFC — its rendered HTML embeds every scheme's
absolute ``.xlsx`` URL directly (the full history, ~1100+ links). So a single
GET of that page gives the whole catalog; we just regex the links for the
requested data month and skip the SPA/AJAX route entirely.

URL pattern (verified for April-2026 data):
    https://www.unionmf.com/docs/default-source/funddetail-downloads/
      fund-portfolio/<monthname>-<YYYY>/
      monthly-portfolio-report-union-<scheme-slug>-<DD>-<MM>-<YYYY>.xlsx
        ?sfvrsn=<cachebuster>

The data month is encoded BOTH in the path folder ("april-2026") AND in the
filename's trailing ``<DD>-<MM>-<YYYY>`` (the portfolio "as on" date, which is
the month-end for monthly schemes — Union publishes April-data files in May
but files them under the April-2026 folder with a 30-04-2026 date). We key
discovery off the data-month folder + verify the filename's month/year, so
next month works unchanged: just pass the new ``ym``.

Note the path historically used a longer folder
(``downloads/scheme-disclosures/portfolios-disclosure/month-portfolios/``)
through ~2024; everything from 2024-04 onward uses the
``funddetail-downloads/fund-portfolio/`` path matched here. We deliberately
match only the current path to avoid colliding with the legacy structure.

The ``?sfvrsn=`` cache-buster is stripped — the bare URL downloads fine.

Scheme name: the printed name is derived from the filename slug (strip the
``monthly-portfolio-report-`` prefix and the trailing date, replace hyphens
with spaces, Title-Case). The downstream fuzzy matcher canonicalizes
(uppercases + strips punctuation) before matching against scheme_master, so
this derived name resolves regardless of casing/spacing cosmetics.

Excel layout (validated against Union Flexi Cap Fund, April 2026)
----------------------------------------------------------------
- Single sheet named after the scheme (e.g. 'Flexi Cap Fund').
- Rows 1-7: AMC banner + "MONTHLY PORTFOLIO STATEMENT OF ..." + SEBI scheme
  description.
- Row 8: header — col C='Name of the Instrument', col D='ISIN',
  col E='Rating / Industry', col F='Quantity',
  col G='Market value (Rs. in Lakhs)', col H='% to NAV', col I='YTM %'.
  (Cols K+ carry separate Industry/Sector/Top-10-Issuer summary tables whose
  own '% to NAV' sub-headers sit on row 9; the generic header detector locks
  onto row 8's ISIN+name+weight triple first, so the summary columns are
  never mistaken for the holdings table.)
- Row 10+: section banners ('EQUITY & EQUITY RELATED',
  'a) Listed/awaiting listing on Stock Exchanges', ...) interleaved with
  ISIN-bearing holding rows. '% to NAV' is stored as a PERCENT (4.49 == 4.49%).

This is the canonical SEBI layout, so ``parse_sebi_excel`` (sheet_index=0)
auto-detects the header row, the ISIN/name/weight columns, and the
percent weight unit — no bespoke ``parse_excel`` needed. ``fetch_excel`` is
inherited too (plain cached GET).
"""

from __future__ import annotations

import re

from mfs.ingest.holdings._generic import GenericHoldingsAdapter, discover_xlsx_links
from mfs.ingest.holdings._registry import register_adapter
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = "https://www.unionmf.com/about-us/downloads/monthly-portfolio"

_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def _folder_token(ym: str) -> str:
    """data_ym='2026-04' -> 'april-2026' (the data-month path folder)."""
    y, m = map(int, ym.split("-"))
    return f"{_MONTHS[m - 1]}-{y:04d}"


def _url_pattern(ym: str) -> re.Pattern:
    """Compile the per-month xlsx URL regex.

    Anchored on the data-month folder; captures the full URL (group 'url')
    and the filename's trailing date so ``_scheme_from_filename`` can confirm
    the date falls in ``ym`` before accepting the link. The ``?sfvrsn=``
    cache-buster is matched-and-discarded.
    """
    folder = re.escape(_folder_token(ym))
    return re.compile(
        r"(?P<url>https://www\.unionmf\.com/docs/default-source/"
        r"funddetail-downloads/fund-portfolio/" + folder + r"/"
        r"monthly-portfolio-report-[^\"']+?\.xlsx)",
        re.IGNORECASE,
    )


# Filename shape: monthly-portfolio-report-<slug>-<DD>-<MM>-<YYYY>.xlsx
# The slug may contain hyphens, parentheses and spaces (e.g. the FMP series).
_FILENAME_RE = re.compile(
    r"^monthly-portfolio-report-(?P<slug>.+?)-"
    r"(?P<d>\d{1,2})-(?P<mo>\d{2})-(?P<yr>\d{4})\.xlsx$",
    re.IGNORECASE,
)


def _make_scheme_from_filename(ym: str):
    """Build the ``scheme_from_filename`` callback bound to data month ``ym``.

    Returns the Title-Cased printed scheme name, or None to skip a file whose
    filename date isn't in ``ym`` (defends against a stray cross-month link in
    the same folder).
    """
    want_y, want_m = map(int, ym.split("-"))

    def _scheme_from_filename(filename: str) -> str | None:
        m = _FILENAME_RE.match(filename)
        if not m:
            return None
        if int(m.group("mo")) != want_m or int(m.group("yr")) != want_y:
            return None
        slug = m.group("slug")
        # Title-Case the hyphen/space-delimited slug into a printed name.
        # Casing/punctuation is irrelevant downstream (the matcher uppercases
        # + strips punctuation), but Title-Case keeps logs/debug readable.
        name = re.sub(r"\s+", " ", slug.replace("-", " ")).strip()
        return name.title()

    return _scheme_from_filename


@register_adapter
class UnionHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "union"
    source_label = "Union Mutual Fund"

    # Per-scheme files put the portfolio on sheet 0 in standard SEBI layout
    # with percent weight units — parse_sebi_excel handles both. fetch_excel is
    # the inherited plain cached GET.
    sheet_index: int | None = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Scrape the monthly-portfolio HTML and return
        {printed_scheme_name: absolute_excel_url} for data month ``ym``."""
        out = discover_xlsx_links(
            _DISCLOSURE_PAGE,
            _url_pattern(ym),
            _make_scheme_from_filename(ym),
        )
        log.info("holdings.union.discover", ym=ym, n_schemes=len(out))
        return out
