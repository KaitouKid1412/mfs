"""Bajaj Finserv Mutual Fund — factsheet adapter (Phase 3 PTR coverage).

Calibrated against the combined monthly factsheet reporting data **as on
30 April 2026** (``data/raw/factsheets/bajaj_finserv/2026-04.pdf``, ~7.9 MB,
61 pages). That file is the AMC's "May 2026" factsheet — like HDFC, Bajaj
publishes one calendar month after the data month, so the *April-end* data we
want lives in the *May* issue. ``build_url``/``fetch`` therefore resolve the
**publish month = data month + 1** (see ``_publish_ym``).

URL resolution (the hard part)
------------------------------
There is **no deterministic PDF URL**. Bajaj's downloads page is a WordPress
site whose factsheet filenames have churned every few months and now live on
a CDN under an upload-date directory with irregular names, e.g.::

    .../wp-content/uploads/2026/05/Bajaj-Finserv-Factsheet-May-2026-2.pdf   (Apr data)
    .../wp-content/uploads/2026/04/Bajaj-Finserv-Factsheet_April.pdf        (Mar data)
    .../wp-content/uploads/2025/08/bajaj_finserv_factsheet_january_2026.pdf  (Dec data)

The canonical resolution path is the page's own AJAX endpoint
(``admin-ajax.php`` action ``bajaj_get_downloads`` exposed by the
``bajaj-downloads`` plugin). We:

1. GET ``/downloads`` and scrape the nonce from the plugin's localized JS
   object ``var bajajDownloads = {... "nonce":"<hex>" ...}``. (The page also
   embeds other plugins' nonces — only the ``bajajDownloads`` one validates
   this action; the nonce is not cookie-bound for anonymous users.)
2. POST the action with ``section_id=744`` (the "Download Factsheet"
   accordion), ``year=<Indian FY of publish date>`` (e.g. ``2026-27``), and
   ``month=<publish month full name>`` (e.g. ``May``).
3. Parse the single PDF ``href`` out of the returned HTML fragment.

The AMC's default ``mfs-pipeline/...`` UA is fine here (no WAF on the AJAX
endpoint), but the CDN serves the PDF directly with that UA too. We use a
browser UA defensively. Any failure (page down, nonce gone, no document for
the month) raises ``IngestError`` — we never guess a URL (the filename hash
is non-deterministic).

Layout findings driving the PTR parser
--------------------------------------
* Each scheme prints on its own first page whose **first non-empty line** is
  the printed scheme name, prefixed ``Bajaj Finserv `` (e.g. ``Bajaj Finserv
  Large Cap Fund``). ETF / index / debt pages share that prefix and are
  harmless — they simply don't carry an Equity Turnover line so they yield
  nothing.

* PTR is printed under a ``Portfolio Turnover (Times)`` header as
  ``Equity Turnover 1.41`` — a **fraction** ("Times"), so we pass it through
  with NO /100 conversion. The page also prints ``Total Portfolio Turnover``
  (equity + debt + derivatives); we deliberately match only the ``Equity
  Turnover`` label (the ``Total`` line has no ``Equity`` token so the regex
  can't grab it) because equity turnover is the Phase-2 trade-cost driver.

* On a few pages the ``Equity Turnover`` line carries left-column
  market-cap-allocation bleed in ``extract_text`` (e.g. ``44.18% Large Cap
  Equity Turnover 1.06``), but the value always immediately follows the
  ``Equity Turnover`` label, so the anchored text regex handles every case —
  no word-position fallback is needed.

Calibration counts on 2026-04 (April-end data, the May issue)
-------------------------------------------------------------
* 24 ``Bajaj Finserv ...`` scheme pages detected.
* 10 PTR records parsed (equity / hybrid funds that print the metric):
  Large Cap, Flexi Cap, Large & Mid Cap, Multi Cap, Consumption,
  Healthcare, ELSS Tax Saver, Balanced Advantage, Multi Asset Allocation,
  Arbitrage. Values 0.47-1.64 — sane fractions.
* 3 equity schemes legitimately omit PTR (no Equity/Total Turnover value
  printed at all, only the market-cap allocation): Small Cap, Banking &
  Financial Services, Equity Savings — all recent NFOs / hybrid. We drop
  them (fail-fast: no half-data). The remaining EQ=0 pages are debt / liquid
  / ETF / index funds where the metric is meaningless.

Holdings are intentionally not extracted here — the ISIN-tagged Excel path is
the canonical holdings source. ``parse_holdings`` returns ``()``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import httpx
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

# A browser-style UA — defensive against any CDN/WAF UA filtering on the
# media host (the AMFI default UA is otherwise accepted on the AJAX endpoint).
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# WordPress AJAX endpoint + the "Download Factsheet" accordion section id and
# the action exposed by the bajaj-downloads plugin.
_DOWNLOADS_PAGE = "https://www.bajajamc.com/downloads"
_AJAX_URL = "https://www.bajajamc.com/wp-admin/admin-ajax.php"
_FACTSHEET_SECTION_ID = "744"
_AJAX_ACTION = "bajaj_get_downloads"

# Nonce localized by the bajaj-downloads plugin specifically (other plugins on
# the page expose their own, non-validating nonces).
_NONCE_RE = re.compile(
    r"var\s+bajajDownloads\s*=\s*\{[^}]*?\"nonce\"\s*:\s*\"([a-f0-9]+)\"",
    re.IGNORECASE,
)
_HREF_PDF_RE = re.compile(r"href=\"([^\"]+\.pdf)\"", re.IGNORECASE)


def _publish_ym(data_ym: str) -> tuple[int, int]:
    """data month YYYY-MM -> (publish_year, publish_month) = data + 1 month.

    Bajaj's "<Month> <Year>" factsheet reports the *previous* month's data, so
    the April-end (2026-04) figures live in the May 2026 issue.
    """
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return y + 1, 1
    return y, m + 1


def _fiscal_year_label(year: int, month: int) -> str:
    """Indian fiscal year (Apr-Mar) for (year, month) as 'YYYY-YY'.

    The downloads filter buckets factsheets by FY. April-December of year Y
    belong to FY ``Y-(Y+1)``; January-March belong to FY ``(Y-1)-Y``.
    """
    start = year if month >= 4 else year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def _resolve_factsheet_url(ym: str) -> str:
    """Resolve the combined factsheet PDF URL for data month ym='YYYY-MM'.

    Scrapes the plugin nonce from the downloads page, then queries the
    ``bajaj_get_downloads`` AJAX action for the publish month (data + 1) under
    the matching Indian fiscal year, and returns the PDF href from the HTML
    fragment.

    Raises IngestError on any failure — we never fabricate a URL (the CDN
    filename is non-deterministic).
    """
    pub_year, pub_month = _publish_ym(ym)
    month_name = _MONTH_NAMES[pub_month - 1]
    fy = _fiscal_year_label(pub_year, pub_month)

    try:
        with httpx.Client(
            timeout=60.0,
            headers={"User-Agent": _BROWSER_UA, "Accept": "*/*"},
            follow_redirects=True,
        ) as c:
            page = c.get(_DOWNLOADS_PAGE)
            page.raise_for_status()
            nm = _NONCE_RE.search(page.text)
            if not nm:
                raise IngestError(
                    "bajaj_finserv: could not find the bajajDownloads nonce on "
                    f"{_DOWNLOADS_PAGE} (page layout may have changed)."
                )
            nonce = nm.group(1)

            resp = c.post(
                _AJAX_URL,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{_DOWNLOADS_PAGE}?factsheet",
                },
                data={
                    "action": _AJAX_ACTION,
                    "nonce": nonce,
                    "section_id": _FACTSHEET_SECTION_ID,
                    "year": fy,
                    "month": month_name,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except IngestError:
        raise
    except Exception as e:  # noqa: BLE001
        raise IngestError(
            f"bajaj_finserv: factsheet URL resolution failed for {ym} "
            f"(publish {month_name} {pub_year}, FY {fy}): {e}"
        ) from e

    if not isinstance(data, dict) or not data.get("success"):
        raise IngestError(
            "bajaj_finserv: AJAX returned no document (nonce rejected or "
            f"unknown filter) for {month_name} {pub_year}, FY {fy}: {data!r}"
        )
    html = ((data.get("data") or {}) if isinstance(data.get("data"), dict) else {}).get("html", "")
    urls = _HREF_PDF_RE.findall(html)
    if not urls:
        raise IngestError(
            f"bajaj_finserv: no PDF found for {month_name} {pub_year} "
            f"(FY {fy}). The issue may not be published yet."
        )
    return urls[0]


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------

_PREFIX_RE = re.compile(r"^Bajaj\s+Finserv\s+\S", re.IGNORECASE)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name (first non-empty line) for a scheme
    page, or None for cover / TOC / disclosure pages.

    Every per-scheme page's first non-empty line is the ``Bajaj Finserv
    <name>`` banner. Non-scheme pages (cover, index, market outlook,
    disclaimers) don't lead with that prefix.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not _PREFIX_RE.match(first):
        return None
    name = re.sub(r"\s+", " ", first).strip()
    return name or None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Bajaj prints PTR as a FRACTION ("Times"): ``Equity Turnover 1.41`` means a
# ratio of 1.41. Storage is also a fraction, so we pass through (NO /100).
# The value always immediately follows the ``Equity Turnover`` label even when
# left-column market-cap bleed prefixes the line, so a single anchored regex
# is sufficient. ``Total Portfolio Turnover`` has no ``Equity`` token and is
# never matched.
_PTR_RE = re.compile(r"Equity\s+Turnover\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


@register_adapter
class BajajFinservAdapter(ManagerAdapter):
    """Bajaj Finserv Mutual Fund factsheet adapter.

    ``amc_slug = 'bajaj_finserv'`` matches ``scheme_master.amc_code``
    directly, so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "bajaj_finserv"
    source_label = "Bajaj Finserv Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical combined-factsheet PDF URL for data month
        ym='YYYY-MM'.

        Bajaj's CDN filenames are non-deterministic, so the URL is resolved
        live via the downloads-page AJAX endpoint for the publish month
        (data month + 1). Raises ``IngestError`` if the issue isn't
        published yet or the endpoint changes shape.
        """
        return _resolve_factsheet_url(ym)

    def fetch(self, ym: str) -> Path:
        """Resolve and download the data-month factsheet to the cache path.

        Overrides the base ``fetch`` because (a) ``build_url`` performs a
        network resolution we don't want to repeat on cache hits, and (b) we
        carry a browser UA through to the media CDN. Re-uses an existing
        cached PDF unconditionally — delete the file to force a re-fetch.
        """
        from mfs import paths

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        url = self.build_url(ym)
        try:
            with httpx.Client(
                timeout=120.0,
                headers={"User-Agent": _BROWSER_UA, "Accept": "*/*"},
                follow_redirects=True,
            ) as c:
                r = c.get(url)
                r.raise_for_status()
                payload = r.content
        except Exception as e:  # noqa: BLE001
            raise IngestError(
                f"bajaj_finserv: failed to download {url}: {e}"
            ) from e
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.rename(out)
        return out

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
                m = _PTR_RE.search(text)
                if not m:
                    # Debt / liquid / ETF / index pages omit the block, as do
                    # 3 recent equity NFOs (Small Cap, Banking & Financial
                    # Services, Equity Savings) — all legitimately yield
                    # nothing (fail-fast: no half-data).
                    continue
                try:
                    ptr = float(m.group(1))
                except ValueError:
                    continue
                # Already a fraction ("Times") — pass through. Drop NaN /
                # non-positive garbage.
                if ptr != ptr or ptr <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr,
                    source_amc=self.amc_slug,
                )

    # ------------------------------------------------------------------
    # Holdings — deferred to the ISIN-tagged Excel path. Returns ().
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
