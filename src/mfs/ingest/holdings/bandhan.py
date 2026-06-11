"""Bandhan Mutual Fund monthly portfolio holdings adapter (Phase 5).

Bandhan publishes one SEBI-format Excel per scheme per month — the ideal
shape for ``GenericHoldingsAdapter`` + the shared ``parse_sebi_excel``
(sheet 0 carries the equity table in the canonical ISIN / Name / % to NAV
layout, percent units).

Discovery is the only bespoke part. The disclosures page
(https://bandhanmutual.com/downloads/disclosures) is a React SPA whose
server-rendered HTML carries NO .xlsx links; the per-scheme table is
hydrated client-side from a WordPress JSON API on the AMC's headless CMS:

    GET https://cmsnew.bandhanmutual.com
        /wp-json/finance-api/v1/posts/disclosures?posts_per_page=2500

(endpoint string lifted from the SPA's main.js bundle, key
``getNewAdditionalDisclosureApi``). It returns the WHOLE disclosures
catalogue — fortnightly + monthly/half-yearly, all financial years — as
one JSON array under ``data``. Each record looks like:

    {"title": "Monthly and Half-yearly - Bandhan Flexi Cap Fund 30 April 2026",
     "date":  "2026-05-08 21:28:..",                  # PUBLISH timestamp
     "acf_fields": {
        "disclosures_type": "Monthly and Half-yearly Disclosures",
        "funds_mapping": {"post_title": "Bandhan Flexi Cap Fund", ...},
        "disclosure_files": [
           {"document_name": "...",
            "document_link": {"url": "https://storage.googleapis.com/.../
                bandhan-flexi-cap-fund_..._30-april-2026.xlsx", ...}}]}}

The DATA month-end ("30 April 2026") is printed in the record ``title`` (and
``document_name``); the record ``date`` is the PUBLISH timestamp (April data
is published in May, the same +1 offset as HDFC/SBI). We therefore key off
the data date in the title, not the publish date, so the adapter is
parameterised by the data month ``ym`` and next month just works.

Discovery filters to ``disclosures_type == "Monthly and Half-yearly
Disclosures"`` (dropping the Fortnightly entries), extracts the
``<day> <Month> <Year>`` token from the title, keeps records whose data
month equals ``ym``, and maps the scheme name to its .xlsx URL. We prefer
``funds_mapping.post_title`` for the printed scheme name (clean canonical
form, e.g. "Bandhan Flexi Cap Fund"); when that is absent we fall back to
the title with the "Monthly and Half-yearly - " prefix and trailing date
stripped. The .xlsx URL is already absolute (Google Cloud Storage), so no
absolutising is needed.

Per-scheme Excel layout (validated against Bandhan Flexi Cap, April 2026):
- Sheet 0 carries the portfolio in the canonical SEBI layout: a header row
  with ISIN / Name of the Instrument / Industry / Quantity / Market Value /
  % to NAV, then section banners ("Equity & Equity related", "(a) Listed /
  awaiting listing on Stock Exchanges", "Money Market Instruments", …)
  interleaved with ISIN-bearing holding rows. ``% to NAV`` is in PERCENT
  units.

Because the per-scheme sheet IS the canonical SEBI layout in percent units,
we inherit ``GenericHoldingsAdapter`` wholesale: ``parse_sebi_excel`` auto-
detects the header row, the ISIN/name/weight columns, and the weight unit;
``fetch_excel`` does the standard cached download. Validated on Bandhan
Flexi Cap: 87 equity ISIN rows summing to ~95%.
"""

from __future__ import annotations

import json
import re

from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import fetch_bytes
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURES_API = (
    "https://cmsnew.bandhanmutual.com"
    "/wp-json/finance-api/v1/posts/disclosures?posts_per_page=2500"
)

_MONTHLY_TYPE = "Monthly and Half-yearly Disclosures"

_MONTHS = {
    name.lower(): i
    for i, name in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}

# Pull a "<day> <Month> <Year>" date token out of a disclosure title.
# Titles come in two shapes — "… - Bandhan Flexi Cap Fund 30 April 2026" and
# "… - Bandhan FTP Series 179 (3652 days) - 30 April 2026" — so we search for
# the token anywhere rather than anchoring to the end.
_TITLE_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")

# Strip the leading disclosure-type prefix and trailing date from a title to
# recover the scheme name (fallback only; we prefer funds_mapping.post_title).
_TITLE_PREFIX_RE = re.compile(
    r"^Monthly\s+and\s+Half[- ]?[Yy]early\s*-\s*", re.IGNORECASE
)


def _title_data_ym(title: str) -> str | None:
    """Extract the DATA month (YYYY-MM) printed in a disclosure title.

    Returns None if no recognizable ``<day> <Month> <Year>`` token is found.
    """
    m = _TITLE_DATE_RE.search(title)
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower())
    if month is None:
        return None
    return f"{int(m.group(3)):04d}-{month:02d}"


def _scheme_name_from_title(title: str) -> str:
    """Fallback scheme name: strip the type prefix and the trailing date."""
    s = _TITLE_PREFIX_RE.sub("", title).strip()
    # Drop the trailing data-date token (with or without a leading dash).
    s = _TITLE_DATE_RE.sub("", s).strip().rstrip("-").strip()
    return re.sub(r"\s+", " ", s)


@register_adapter
class BandhanHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "bandhan"
    source_label = "Bandhan Mutual Fund"
    # Per-scheme files put the portfolio on sheet 0 in standard SEBI layout.
    sheet_index = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {printed_scheme_name: absolute .xlsx URL} for data month
        ``ym``.

        Pulls the full disclosures catalogue from the headless-CMS JSON API,
        keeps the monthly/half-yearly records whose title data-date falls in
        ``ym``, and maps each scheme name to its first .xlsx document link.
        """
        raw = fetch_bytes(_DISCLOSURES_API).decode("utf-8", errors="replace")
        payload = json.loads(raw)
        records = payload.get("data")
        if not isinstance(records, list):
            raise ValueError(
                "Bandhan disclosures API did not return a 'data' array"
            )

        out: dict[str, str] = {}
        for rec in records:
            acf = rec.get("acf_fields") or {}
            if acf.get("disclosures_type") != _MONTHLY_TYPE:
                continue
            title = rec.get("title")
            if not isinstance(title, str):
                continue
            if _title_data_ym(title) != ym:
                continue

            # Scheme name: prefer the clean canonical post_title.
            name = (acf.get("funds_mapping") or {}).get("post_title")
            if not isinstance(name, str) or not name.strip():
                name = _scheme_name_from_title(title)
            name = re.sub(r"\s+", " ", name).strip()
            if not name:
                continue

            url = _first_xlsx_url(acf.get("disclosure_files"))
            if not url:
                continue
            # First occurrence wins on the rare duplicate.
            out.setdefault(name, url)

        log.info("holdings.bandhan.discover", ym=ym, n_schemes=len(out))
        return out


def _first_xlsx_url(disclosure_files: object) -> str | None:
    """Pick the first .xls/.xlsx document link from a record's file list."""
    if not isinstance(disclosure_files, list):
        return None
    for f in disclosure_files:
        if not isinstance(f, dict):
            continue
        link = f.get("document_link") or {}
        url = link.get("url") if isinstance(link, dict) else None
        if isinstance(url, str) and url.lower().split("?", 1)[0].endswith(
            (".xlsx", ".xls")
        ):
            return url.strip()
    return None
