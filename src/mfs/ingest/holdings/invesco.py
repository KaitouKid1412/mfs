"""Invesco Mutual Fund monthly portfolio holdings adapter (Phase 5).

Invesco publishes one SEBI-format Excel per scheme per month. The
"Complete Monthly Holdings" page is a JS SPA:

    https://www.invescomutualfund.com/literature-and-form?tab=Complete

The server-rendered HTML carries NO .xlsx links — the per-scheme table is
injected client-side from a small JSON API. Discovery therefore drives that
API directly (SBI-style), no headless browser needed.

Backing API (reverse-engineered from the page's jQuery + jsRender block):

    GET /api/ClassificationCompleteMonthlyHoldings?page=Holding
        -> [{"FundClassification":"Equity",
             "FunClassificationValue":"equity", ...}, ...]
        the list of fund "classifications" (equity / hybrid / fixed-income /
        fund-of-funds / exchange-traded-fund / fixed-maturity-plans / ...).

    GET /api/CompleteMonthlyHoldings?year=<YYYY>&classification=<value>
        -> one record per scheme in that classification & DATA year:
           {"Name": "Invesco India Flexi Cap Fund",
            "JanUrl": "...", "FebUrl": "...", ..., "DecUrl": "...",
            "JanName": "01/26", ..., "AprName": "04/26", ...}
        <Mon>Url is the absolute .xlsx for that DATA month (empty string when
        not yet published); <Mon>Name encodes the data MM/YY, which we use to
        assert the URL we pick really is the requested data month.

`year` is the DATA year and the month column is the DATA month-end (the
April-2026 portfolio sits under year=2026 / AprUrl with AprName "04/26"),
so there is no publish-month offset to reverse. We iterate every
classification and collect each scheme's month-`ym` URL — that gives the
full catalogue (equity + hybrid + debt) in one pass.

Per-scheme Excel layout (validated against Invesco India Flexi Cap, Apr 2026):
- Sheet 0 named after the scheme ("Flexi Cap"); sheet 1 is the scheme code.
- Rows 0-3: scheme banner + "Monthly Portfolio Statement as on ...".
- Row 4: header — col B="Name of the Instrument", C="ISIN",
  D="Industry*", E="Quantity", F="Market/Fair Value (Rs. in Lakhs)",
  G="% to Net Assets" (PERCENT units, e.g. 5.43), H="YTM".
- Row 5+: section banners ("Equity & Equity related", "(a) Listed /
  awaiting listing on Stock Exchanges", "Money Market Instruments", ...)
  interleaved with ISIN-bearing holding rows.

Because the per-scheme sheet IS the canonical SEBI layout in percent units,
we inherit ``GenericHoldingsAdapter`` wholesale: ``parse_sebi_excel`` auto-
detects the header row, the ISIN/name/weight columns, and the weight unit;
``fetch_excel`` does the standard cached download. The only bespoke part is
discovery. Validated on Invesco India Flexi Cap: equity ISIN rows summing to
~93%.
"""

from __future__ import annotations

import json
import re

from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.io.http import fetch_bytes
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_BASE = "https://www.invescomutualfund.com"
_CLASSIFICATION_API = (
    _BASE + "/api/ClassificationCompleteMonthlyHoldings?page=Holding"
)
_HOLDINGS_API = _BASE + "/api/CompleteMonthlyHoldings"

# Month index (1..12) -> the JSON field prefix Invesco uses (JanUrl/FebUrl/…).
_MONTH_PREFIX = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _fetch_json(url: str) -> list[dict]:
    """GET a JSON array from the Invesco API. The endpoints return a JSON
    body even though no explicit Accept header is sent by the page."""
    raw = fetch_bytes(url).decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"Invesco API {url} did not return a JSON array")
    return data


def _expected_mon_name(ym: str) -> str:
    """data_ym='2026-04' -> '04/26' — the value Invesco prints in <Mon>Name.

    Used to assert the URL we picked truly is the requested data month, so a
    column-misalignment on Invesco's side can never silently feed us the
    wrong month's file.
    """
    y, m = map(int, ym.split("-"))
    return f"{m:02d}/{y % 100:02d}"


@register_adapter
class InvescoHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "invesco"
    source_label = "Invesco Mutual Fund"

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {printed_scheme_name: absolute .xlsx URL} for data month
        ``ym`` across every fund classification.

        For each classification we hit the holdings API with the data
        ``year``, then read the month-``ym`` URL column off each scheme
        record, verifying the sibling ``<Mon>Name`` matches ``MM/YY`` before
        accepting it. Schemes whose month URL is empty (not yet published)
        are skipped.
        """
        y, m = map(int, ym.split("-"))
        prefix = _MONTH_PREFIX[m - 1]
        url_field = f"{prefix}Url"
        name_field = f"{prefix}Name"
        expected_name = _expected_mon_name(ym)

        out: dict[str, str] = {}
        for cls in self._classifications():
            try:
                records = _fetch_json(
                    f"{_HOLDINGS_API}?year={y}&classification={cls}"
                )
            except Exception as e:  # noqa: BLE001 — one bad class shouldn't kill all
                log.warning(
                    "holdings.invesco.classification_failed",
                    ym=ym, classification=cls, err=str(e),
                )
                continue
            for rec in records:
                name = rec.get("Name")
                url = rec.get(url_field)
                if not isinstance(name, str) or not isinstance(url, str):
                    continue
                name = re.sub(r"\s+", " ", name).strip()
                url = url.strip()
                if not name or not url:
                    continue
                # Guard against a shifted month column: the sibling name cell
                # must encode the requested MM/YY.
                mon_name = rec.get(name_field)
                mon_name_s = mon_name.strip() if isinstance(mon_name, str) else ""
                if mon_name_s not in ("", "-") and mon_name_s != expected_name:
                    log.warning(
                        "holdings.invesco.month_name_mismatch",
                        ym=ym, scheme=name, field=name_field,
                        got=mon_name_s, want=expected_name,
                    )
                    continue
                out.setdefault(name, url)

        log.info("holdings.invesco.discover", ym=ym, n_schemes=len(out))
        return out

    def _classifications(self) -> list[str]:
        """The list of classification slugs to enumerate. Pulled live from
        the API so a new classification (e.g. a new product line) is picked
        up automatically; falls back to the known set if the call fails."""
        try:
            data = _fetch_json(_CLASSIFICATION_API)
        except Exception as e:  # noqa: BLE001
            log.warning("holdings.invesco.classification_list_failed", err=str(e))
            return [
                "equity", "hybrid", "fixed-income", "fund-of-funds",
                "exchange-traded-fund", "fixed-maturity-plans",
            ]
        vals: list[str] = []
        for c in data:
            v = c.get("FunClassificationValue")
            if isinstance(v, str) and v.strip():
                vals.append(v.strip())
        return vals or ["equity", "hybrid", "fixed-income"]
