"""Helios Mutual Fund monthly portfolio holdings adapter (Phase 5).

Helios publishes ONE SEBI-format Excel per scheme per month — the ideal shape
for ``GenericHoldingsAdapter`` + the shared ``parse_sebi_excel``.

The disclosures page (https://www.heliosmf.in/portfolio-disclosure/) is a
WordPress page whose static HTML directly embeds every portfolio Excel link
as a plain absolute URL — so a single curl-with-browser-UA scrape gives us
the whole catalog. This is the HDFC discovery shape (links-in-HTML), so we
reuse ``discover_xlsx_links``.

URL convention (WordPress media library):
    https://www.heliosmf.in/wp-content/uploads/{PUBLISH_YYYY}/{PUBLISH_MM}/
        Helios-<Scheme>-Fund-Monthly-Portfolio-as-on-<DDth>-<Month>-<YYYY>.xls[x]

Two date components appear in the URL and they are NOT the same month:
- The ``/uploads/YYYY/MM/`` path segment is the *publish* month (the month
  Helios uploaded the file; April-2026 *data* lands in the 2026/05 folder,
  i.e. publish = data + 1, same offset as HDFC/SBI/Mirae).
- The ``as-on-<DD>-<Month>-<YYYY>`` token in the *filename* is the
  *data-month-end* date. We key discovery off the FILENAME date because it
  is the authoritative data-month anchor (the upload-folder month has
  drifted historically — e.g. some files land a folder early/late), and it
  survives Helios re-uploading a corrected file into a different folder.

We deliberately match only ``...-Monthly-Portfolio-as-on-...`` files and
skip ``Fortnightly`` / ``Half-Yearly`` portfolios and the non-portfolio
spreadsheets (CAMS-Branches, Branch-Address-Master) on the same page.

Filename quirks handled:
- Extension is ``.xls`` for older months and ``.xlsx`` for newer ones; both
  bodies are real OOXML (validated 2026-04). We accept either extension.
- The day carries an English ordinal suffix (``30th`` / ``31st`` / ``29th``).
- Helios has historically typo'd "Monthly" as "Monthtly" in a few overnight
  filenames; we tolerate the optional extra ``t``.
- A trailing ``-1`` disambiguator sometimes appears before the extension
  (e.g. ``...-31st-March-2024-1.xls``) when a file is re-uploaded.

Excel layout (validated against Helios Flexi Cap Fund, April 2026):
- Single sheet named after the scheme code (e.g. 'HFCF').
- Rows 1-3: "Helios Mutual Fund" banner, "SCHEME NAME :", "PORTFOLIO
  STATEMENT AS ON : 2026-04-30".
- Row 5: column header — Name of the Instrument / Issuer | ISIN |
  Rating / Industry^ | Quantity | Market value (Rs. in Lakhs) |
  % to AUM | YTM %.
- Row 7+: section banners ('EQUITY & EQUITY RELATED', 'Listed/awaiting
  listing on Stock Exchanges') interleaved with ISIN-bearing holding rows;
  % to AUM is in percent units.

This is the canonical SEBI layout, so ``parse_sebi_excel`` (sheet_index=0)
auto-detects the header, ISIN/name/weight columns, and the percent unit —
no bespoke parse_excel needed.
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

_DISCLOSURE_PAGE = "https://www.heliosmf.in/portfolio-disclosure/"

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_ALT = "|".join(_MONTH_NAMES)

# Full monthly-portfolio Excel URL. We anchor on the heliosmf.in WordPress
# uploads path and the "Monthly-Portfolio-as-on-<DD><ord>-<Month>-<YYYY>"
# filename. The filename's data-month date (day/month/year) is captured so
# discovery can keep only the requested ym; we tolerate the "Monthtly" typo,
# either .xls/.xlsx extension, and an optional "-N" re-upload disambiguator.
_URL_RE = re.compile(
    r"(?P<url>https://www\.heliosmf\.in/wp-content/uploads/"
    r"\d{4}/\d{2}/"
    r"Helios-[A-Za-z0-9\-]+?-Month(?:t)?ly-Portfolio-as-on-"
    r"\d{1,2}(?:st|nd|rd|th)-"
    rf"(?:{_MONTH_ALT})-"
    r"\d{4}"
    r"(?:-\d+)?\.xlsx?)",
    re.IGNORECASE,
)

# Pull the data-month date + scheme name out of a matched filename.
_FILENAME_RE = re.compile(
    r"^Helios-(?P<scheme>[A-Za-z0-9\-]+?)-Month(?:t)?ly-Portfolio-as-on-"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)-"
    rf"(?P<month>{_MONTH_ALT})-"
    r"(?P<year>\d{4})"
    r"(?:-\d+)?\.xlsx?$",
    re.IGNORECASE,
)


def _data_ym_from_filename(filename: str) -> str | None:
    """Reverse-derive data_ym ('YYYY-MM') from a Helios monthly filename's
    'as-on' date, or None if it isn't a monthly-portfolio file."""
    m = _FILENAME_RE.match(filename)
    if not m:
        return None
    month_l = m.group("month").lower()
    try:
        month = next(
            i for i, n in enumerate(_MONTH_NAMES, start=1)
            if n.lower() == month_l
        )
    except StopIteration:
        return None
    return f"{int(m.group('year')):04d}-{month:02d}"


def _scheme_from_filename(filename: str) -> str | None:
    """Turn a Helios monthly filename into the printed scheme name, e.g.
    'Helios-Flexi-Cap-Fund-Monthly-Portfolio-as-on-30th-April-2026.xlsx'
    -> 'Helios Flexi Cap Fund'. Returns None for non-monthly files."""
    m = _FILENAME_RE.match(filename)
    if not m:
        return None
    raw = m.group("scheme")  # e.g. 'Flexi-Cap-Fund' or 'Large-Mid-Cap-Fund'
    name = raw.replace("-", " ").strip()
    # Restore the ampersand Helios drops from the filesystem-safe filename:
    # "Large Mid Cap Fund" -> "Large & Mid Cap Fund" to match scheme_master.
    name = re.sub(
        r"\bLarge Mid Cap\b", "Large & Mid Cap", name, flags=re.IGNORECASE,
    )
    return f"Helios {name}"


@register_adapter
class HeliosHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "helios"
    source_label = "Helios Mutual Fund"
    # Per-scheme files put the portfolio on sheet 0 in standard SEBI layout.
    sheet_index = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Scrape the WordPress disclosures HTML and keep only monthly-
        portfolio Excels whose filename 'as-on' date is in data month `ym`."""

        def scheme_if_month_matches(filename: str) -> str | None:
            if _data_ym_from_filename(filename) != ym:
                return None
            return _scheme_from_filename(filename)

        out = discover_xlsx_links(
            _DISCLOSURE_PAGE, _URL_RE, scheme_if_month_matches,
        )
        log.info("holdings.helios.discover", ym=ym, n_schemes=len(out))
        return out
