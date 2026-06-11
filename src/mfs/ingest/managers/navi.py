"""Navi Mutual Fund — factsheet PTR adapter.

Calibrated against the April 2026 factsheets (data month 2026-04, published
2026-05-11).

URL / discovery
---------------
Navi (navi.com, a WordPress site) does NOT expose a deterministic factsheet
URL: each monthly PDF is uploaded to a CDN host
(``public-assets.prod.navi-tech.in``) with a non-deterministic upload
timestamp embedded in the filename, e.g.::

    https://public-assets.prod.navi-tech.in/navi-website-assests/documents/
        Active_Fund_Apr_26_20260511180410.pdf

So the URL must be RESOLVED at fetch time, not hardcoded. The download page
(``/mutual-fund/downloads/factsheet``) drives a WordPress REST endpoint::

    POST https://navi.com/wp-json/nv/v1/documents
      financial_year = <Indian FY, e.g. "2026-2027">
      value          = <month name, e.g. "April">   (Monthly "duration")
      category       = 867                            (the Factsheet category)
      type           = Monthly
      order          = DESC

which returns the live PDF URLs. ``build_url(ym)`` performs that resolution
and returns the ACTIVE-fund PDF URL (parameterized by ym → FY + month name).

Navi splits its combined factsheet into TWO PDFs:
  * **Active Fund** — Flexi Cap, Large & Midcap, Aggressive Hybrid, Liquid.
  * **Passive Fund** — the index / ETF / FoF range, INCLUDING the index-based
    ``Navi ELSS Tax Saver Nifty 50 Index Fund``.
Both print per-scheme PTR, so ``fetch()`` downloads BOTH (active → canonical
``navi/2026-04.pdf``, passive → sibling ``navi/2026-04_passive.pdf``) and
``parse_ptr`` reads both. The orchestrator dedupes by ``(scheme_code,
as_of_month)`` so emitting from two files is safe.

Layout findings driving the parser
----------------------------------
* PTR is printed as ``Portfolio Turnover Ratio (Times): 0.57``. The
  ``(Times)`` qualifier means the value is ALREADY A FRACTION (0.57 = 57%
  turnover) — we PASS IT THROUGH with no /100 conversion. (Contrast: HDFC
  prints a percent and must divide by 100.)

* The glossary page ("How to read the Factsheet") contains the prose
  ``Portfolio Turnover: ...`` definition but no ``(Times):`` number, so the
  ``(Times)`` anchor in the regex naturally excludes it.

* Scheme name is the page banner: the first non-empty line begins ``Navi``/
  ``NAVI`` and the name runs until the line containing ``Fund``. Index-fund
  banners wrap across two lines (e.g. ``Navi Nifty India`` +
  ``Manufacturing Index Fund``; ``Navi ELSS Tax Saver`` + ``Nifty 50 Index
  Fund``), so we accumulate top lines until one contains ``Fund``.

* The ``ft`` ligature in "Nifty" extracts as the private-use codepoint
  ```` (so "Nifty" → "Niy"). We normalize it back to ``ft``
  before yielding so scheme names match ``scheme_master``.

* PTR / AUM blocks live only on the FIRST page of each multi-page scheme;
  continuation pages repeat the banner but omit the ``(Times):`` line, so
  they're skipped by the regex.

* amc_slug == scheme_master.amc_code == "navi", so NO alias entry in
  ``_scheme_match.py`` is required.

Calibration counts on 2026-04
-----------------------------
* Active PDF: 3 PTR records (Flexi Cap 0.57, Large & Midcap 0.67,
  Aggressive Hybrid 0.62). Liquid Fund omits PTR (debt fund — expected).
* Passive PDF: ~12 PTR records across the index range, including the
  ELSS Tax Saver Nifty 50 Index Fund (0.04).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

import pdfplumber

from mfs.errors import IngestError
from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Navi's download API: WordPress REST endpoint that backs the factsheet
# dropdown. ``category=867`` is the Factsheet category id. The endpoint is
# nonce-gated, so we scrape a fresh ``WP-NONCE`` off the download page first.
_DOWNLOADS_PAGE = "https://navi.com/mutual-fund/downloads/factsheet"
_DOCS_PATH = "nv/v1/documents"
_FACTSHEET_CATEGORY = "867"
# Inline ``navi_property = {...}`` config blob on the download page carries the
# REST base URL and the per-request nonce.
_NAVI_PROPERTY_RE = re.compile(r"navi_property\s*=\s*(\{.*?\})")

# The ``ft`` ligature in "Nifty" extracts as this private-use codepoint.
_LIGATURE_FT = ""


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def _financial_year(ym: str) -> str:
    """data month YYYY-MM → Indian financial year label 'YYYY-YYYY'.

    The Indian FY runs April→March, so April 2026 falls in FY 2026-2027 while
    March 2026 falls in FY 2025-2026.
    """
    y, m = map(int, ym.split("-"))
    start = y if m >= 4 else y - 1
    return f"{start}-{start + 1}"


def _resolve_factsheet_urls(ym: str) -> dict[str, str]:
    """Query Navi's documents API for the data-month factsheet PDFs.

    Returns a dict mapping a normalized key (``"active"`` / ``"passive"``) to
    the live CDN PDF URL. Raises ``IngestError`` when the API is unreachable
    or the month's issue is not yet published (we never guess the
    timestamped URL).
    """
    import httpx

    y, m = map(int, ym.split("-"))
    month_name = _MONTH_NAMES[m - 1]
    fy = _financial_year(ym)
    payload = {
        "financial_year": fy,
        "value": month_name,
        "category": _FACTSHEET_CATEGORY,
        "type": "Monthly",
        "order": "DESC",
    }
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    try:
        # Use one client so the WP cookie set by the page load is replayed on
        # the nonce-gated API call.
        with httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": ua},
        ) as c:
            page = c.get(_DOWNLOADS_PAGE)
            page.raise_for_status()
            pm = _NAVI_PROPERTY_RE.search(page.text)
            if not pm:
                raise IngestError(
                    "navi: could not locate navi_property nonce config on "
                    f"{_DOWNLOADS_PAGE} (page layout changed?)."
                )
            prop = json.loads(pm.group(1))
            rest_url = prop.get("rest_url") or "https://navi.com/wp-json/"
            nonce = prop.get("nonce")
            api_url = rest_url + _DOCS_PATH
            r = c.post(
                api_url,
                data=payload,
                headers={
                    "WP-NONCE": nonce or "",
                    "Referer": _DOWNLOADS_PAGE,
                    "Accept": "application/json",
                },
            )
            r.raise_for_status()
            data = r.json()
    except IngestError:
        raise
    except Exception as e:  # noqa: BLE001
        raise IngestError(
            f"navi: documents API failed for {month_name} {y}: {e}"
        ) from e

    docs = data.get("data") or []
    out: dict[str, str] = {}
    for d in docs:
        title = (d.get("title") or "").lower()
        url = d.get("url")
        # ``url`` may be a string (single file) or a list of {link} objects.
        if isinstance(url, list):
            url = next((o.get("link") for o in url if o.get("link")), None)
        if not url or not str(url).lower().endswith(".pdf"):
            continue
        if "active" in title:
            out["active"] = url
        elif "passive" in title:
            out["passive"] = url
    if "active" not in out:
        raise IngestError(
            f"navi: no Active-Fund factsheet for {month_name} {y} in the "
            f"documents API response ({len(docs)} docs). The issue may not be "
            "published yet."
        )
    return out


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------


def _normalize_ligature(s: str) -> str:
    return s.replace(_LIGATURE_FT, "ft")


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme banner from the top of a scheme page.

    The banner begins ``Navi``/``NAVI`` on the first non-empty line and runs
    until (and including) the line containing the word ``Fund``. Index-fund
    banners wrap across two lines, so we accumulate top lines. Returns
    ``None`` for cover / glossary / disclosure / NAV pages.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not (first.startswith("Navi") or first.startswith("NAVI")):
        return None

    name_parts: list[str] = []
    for ln in lines[:3]:  # banner never spans more than two lines + slack
        # Stop if we hit the scheme description (starts the prose blurb).
        low = ln.lower()
        if low.startswith("an open") or low.startswith("(earlier known"):
            break
        name_parts.append(ln)
        if re.search(r"\bFund\b", ln):
            break
    name = " ".join(name_parts)
    name = _normalize_ligature(name)
    # Drop any "(Earlier known as ...)" parenthetical and trailing blurb that
    # may have bled onto the same physical line as the name.
    name = re.split(r"\s*\(Earlier known", name, maxsplit=1)[0]
    name = re.split(r"\s+An open", name, maxsplit=1)[0]
    name = re.sub(r"\s+", " ", name).strip()
    # Require the banner to actually be a fund name (defends against pages
    # whose first line happens to start with "Navi" but isn't a scheme).
    if "Fund" not in name:
        return None
    return name or None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Navi prints PTR as "Portfolio Turnover Ratio (Times): 0.57". The "(Times)"
# qualifier means it is ALREADY A FRACTION (0.57 == 57% turnover) — pass it
# through with NO /100 conversion. The "(Times)" anchor also excludes the
# glossary page's prose "Portfolio Turnover: ..." definition.
_PTR_RE = re.compile(
    r"Portfolio\s+Turnover\s+Ratio\s*\(Times\)\s*:\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _parse_ptr_from_pdf(pdf_path: Path, source_amc: str) -> Iterable[ParsedPtrRecord]:
    import logging as _logging
    _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
    if not pdf_path.exists():
        return
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            m = _PTR_RE.search(text)
            if not m:
                continue
            scheme = _scheme_name_from_page(text)
            if not scheme:
                continue
            try:
                ptr_value = float(m.group(1))
            except ValueError:
                continue
            # Drop NaN / non-positive — Navi prints a real fraction or omits
            # the block; there is no zero-PTR scheme.
            if ptr_value != ptr_value or ptr_value <= 0:
                continue
            yield ParsedPtrRecord(
                scheme_name_printed=scheme,
                ptr=ptr_value,  # already a fraction — pass through
                source_amc=source_amc,
            )


def _passive_path(active_path: Path) -> Path:
    """Sibling cache path for the Passive-fund PDF next to the Active one."""
    return active_path.with_name(active_path.stem + "_passive.pdf")


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@register_adapter
class NaviAdapter(ManagerAdapter):
    """Navi Mutual Fund factsheet adapter.

    ``amc_slug == 'navi'`` matches ``scheme_master.amc_code`` directly, so no
    alias entry in ``_scheme_match.py`` is needed.
    """

    amc_slug = "navi"
    source_label = "Navi Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical Navi ACTIVE-fund factsheet PDF URL for data
        month ym='YYYY-MM'.

        Navi's CDN filenames embed a non-deterministic upload timestamp, so
        the URL is RESOLVED via the WordPress documents API rather than
        hardcoded. Raises ``IngestError`` if the issue isn't published yet.
        """
        return _resolve_factsheet_urls(ym)["active"]

    def fetch(self, ym: str) -> Path:
        """Download both the Active and Passive factsheet PDFs for ym.

        The Active PDF is cached at the canonical ``navi/<ym>.pdf`` path (and
        returned, since the orchestrator parses that path); the Passive PDF
        is cached at the sibling ``navi/<ym>_passive.pdf``. ``parse_ptr``
        reads both. Re-uses existing cached files; delete them to re-fetch.
        """
        from mfs import paths
        from mfs.io.http import download_to

        active_out = paths.factsheet_raw(self.amc_slug, ym)
        passive_out = _passive_path(active_out)
        if active_out.exists() and passive_out.exists():
            return active_out

        urls = _resolve_factsheet_urls(ym)
        if not active_out.exists():
            download_to(urls["active"], active_out)
        # Passive is best-effort: some months may publish active-only. Its
        # absence must not block the active funds.
        if not passive_out.exists() and "passive" in urls:
            try:
                download_to(urls["passive"], passive_out)
            except Exception as e:  # noqa: BLE001
                log.warning("navi.passive_fetch_failed", ym=ym, error=str(e))
        return active_out

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        # Active PDF (the path the orchestrator handed us).
        yield from _parse_ptr_from_pdf(pdf_path, self.amc_slug)
        # Passive PDF sibling (index / ELSS-index range).
        yield from _parse_ptr_from_pdf(_passive_path(pdf_path), self.amc_slug)

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        # Holdings come from the separate Excel-based path; the factsheet
        # adapter only supplies PTR.
        return ()
