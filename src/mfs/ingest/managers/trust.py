"""Trust Mutual Fund (TRUSTMF) — combined factsheet PTR adapter.

Status as of 2026-05 build: **BLOCKED on download** — see "Download wall"
below. The parser below is written and registered so the adapter is ready
the moment the PDF can be fetched (e.g. via an authenticated/browser fetch
path), but it cannot be calibrated against the real 2026-04 file yet
because every programmatic request for the PDF is intercepted by the
trustmf.com WAF.

Scheme-master linkage
---------------------
``scheme_master.amc_code = 'trust'`` carries DIRECT+GROWTH rows, and the
adapter ``amc_slug`` is also ``'trust'`` — so slug == amc_code and NO alias
is needed in ``_scheme_match.py``. The four ranked equity funds with no PTR
are TRUSTMF Flexi Cap / Mid Cap / Multi Cap / Small Cap. Note the printed
brand is the single token ``TRUSTMF`` (not ``TRUST MF``).

URL pattern
-----------
The combined monthly factsheet is published on the AMC's WordPress media
host one calendar month AFTER the data month:

    https://www.trustmf.com/trustmfsys/wp-content/uploads/
        <publish_yyyy>/<publish_mm>/TRUSTMF-Factsheet-for-<MonthName>-<DataYear>.pdf

Calibration anchor (Google-indexed): the **November 2024** factsheet
(data month 2024-11) is published under ``/2024/12/`` as
``TRUSTMF-Factsheet-for-November-2024.pdf``. So:
  * publish directory = data month + 1 (year rolls over)
  * filename month name + year = the DATA month name + DATA year.
``build_url(ym)`` encodes this shift via ``_publish_ym`` (mirrors hdfc).

Download wall (why this adapter is BLOCKED, 2026-05)
---------------------------------------------------
``www.trustmf.com`` is a Vite/React SPA fronted by a managed-protection /
WAF layer. EVERY request for a ``/trustmfsys/wp-content/uploads/.../*.pdf``
path — regardless of User-Agent, Accept, Referer, session cookie, or Range
header — returns HTTP 200 with a **487-byte React SPA shell**
(``<!doctype html>...<div id="root">``) and ``Content-Type: text/html``,
NOT the PDF. This was reproduced with curl, httpx, and WebFetch, and even
for the previously-Google-indexed Nov-2024 URL. The downloads page itself
is JS-rendered and pulls its file list from a Cosmos CMS API whose base URL
is injected at runtime (``VITE_COSMOSAPIURL``); the resolved file links
still point back to the same walled WordPress host
(``window.open(fileurl)``).

No third-party mirror carries the COMBINED factsheet: AMFI's
download-factsheets page just links back to the walled trustmf.com page,
and ``portal.amfiindia.com/spages/<id>.pdf`` are per-scheme Scheme
Information Documents (54-page SIDs) that do NOT print PTR.

Per the fail-fast invariants (no manual entry, halt on scrape failure),
``fetch()`` validates the magic bytes and raises ``IngestError`` rather
than silently caching the SPA shell as a bogus PDF and yielding garbage.

PTR label / unit (TO BE CONFIRMED against the real PDF)
------------------------------------------------------
Not yet observed because the PDF is unreachable. ``parse_ptr`` therefore
matches ALL documented label variants ("Portfolio Turnover Ratio",
"Portfolio Turnover", "Turnover Ratio", "Equity Turnover", "PTR") and
normalises defensively: a printed ``NN%`` (or a magnitude in the 20-300
band that can only be a percent) is divided by 100; a bare fraction
(0.1-3.0) is passed through. STORAGE UNIT IS A FRACTION (1.27 == 127%).
The unit MUST be pinned with a one-line comment + a fixed branch once the
real factsheet is inspected — the percent/fraction call is the most common
bug in these adapters.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pdfplumber

from mfs.errors import IngestError
from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_MONTH_NAMES_LIST = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _publish_ym(data_ym: str) -> str:
    """data month YYYY-MM -> publish month YYYY-MM (data + 1, year rollover)."""
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{m + 1:02d}"


def _data_month_name(data_ym: str) -> str:
    _, m = data_ym.split("-")
    return _MONTH_NAMES_LIST[int(m) - 1]


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------

# Printed brand is the single token "TRUSTMF". A scheme page's banner line is
# "TRUSTMF <Fund Name> Fund" (e.g. "TRUSTMF Flexi Cap Fund"). Multi-scheme
# index / NAV-summary / disclosure pages are dropped (no per-scheme banner or
# two TRUSTMF tokens on one line).
_SCHEME_BANNER_RE = re.compile(
    r"\bTRUSTMF\s+[A-Za-z0-9 &'’\-/.()]+?\s+Fund\b",
    re.IGNORECASE,
)


def _scheme_name_from_page(text: str) -> str | None:
    if not text:
        return None
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        if line.upper().count("TRUSTMF") != 1:
            continue
        m = _SCHEME_BANNER_RE.search(line)
        if m:
            name = re.sub(r"\s+", " ", m.group(0)).strip()
            # Guard against banner-line bleed swallowing the whole page.
            if len(name.split()) <= 8:
                return name
    return None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Match every documented label variant followed by an optional ':' and a
# number, with or without a trailing '%'. UNIT IS UNCONFIRMED (PDF unreachable):
# normalisation happens in _normalise_ptr, NOT here. Storage is a FRACTION.
_PTR_TEXT_RE = re.compile(
    r"(?:Portfolio\s+Turnover\s+Ratio|Portfolio\s+Turnover|Turnover\s+Ratio|"
    r"Equity\s+Turnover|PTR)\s*:?\s*(\d+(?:\.\d+)?)\s*(%?)",
    re.IGNORECASE,
)


def _normalise_ptr(value: float, had_percent: bool) -> float | None:
    """Return PTR as a FRACTION, or None if it can't be a real equity PTR.

    - An explicit trailing '%' (e.g. "127%") -> divide by 100.
    - No '%': a magnitude in [20, 400] can only be a percent ("127.00") ->
      divide by 100; a magnitude in (0, 3.5] is already a fraction -> pass
      through. (An equity-fund PTR is typically 0.1-3.0; the [20,400] band is
      the percent rendering of that same range.)
    - Anything else (0, or implausibly large) -> drop (fail-fast: half/garbage
      data is worse than no data).
    """
    if value != value or value <= 0:  # NaN or non-positive
        return None
    # Percent -> fraction when flagged with '%' or when the bare magnitude is
    # too large to be anything but a percent rendering of an equity PTR.
    frac = value / 100.0 if had_percent or value > 3.5 else value
    # Final sanity gate on the fraction.
    if 0.0 < frac <= 5.0:
        return frac
    return None


@register_adapter
class TrustAdapter(ManagerAdapter):
    """Trust Mutual Fund (TRUSTMF) factsheet adapter."""

    amc_slug = "trust"
    source_label = "Trust Mutual Fund"

    # -------------------------------------------------------------------
    # URL / fetch
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Canonical combined-factsheet PDF URL for data month ym='YYYY-MM'.

        Published one month after the data month under the publish-month
        directory; filename carries the DATA month name + DATA year.
        """
        publish = _publish_ym(ym)
        month_name = _data_month_name(ym)
        data_year = ym.split("-")[0]
        return (
            "https://www.trustmf.com/trustmfsys/wp-content/uploads/"
            f"{publish.replace('-', '/')}/"
            f"TRUSTMF-Factsheet-for-{month_name}-{data_year}.pdf"
        )

    def fetch(self, ym: str) -> Path:
        """Download + validate the factsheet PDF.

        The trustmf.com host returns a React SPA shell (HTML) for any
        programmatic PDF request, so we MUST verify the magic bytes and
        fail fast — caching the SPA shell as a fake PDF would feed garbage
        into the parser.
        """
        from mfs import paths

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists() and out.read_bytes()[:5] == b"%PDF-":
            return out

        url = self.build_url(ym)
        try:
            import httpx

            with httpx.Client(
                follow_redirects=True,
                timeout=60.0,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/pdf,application/octet-stream,*/*",
                    "Referer": "https://www.trustmf.com/downloads?activeTab=factsheets",
                },
            ) as c:
                r = c.get(url)
        except Exception as e:  # noqa: BLE001
            raise IngestError(f"trust factsheet fetch failed for {ym}: {e}") from e

        data = r.content
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or not data[:5] == b"%PDF-":
            raise IngestError(
                f"trust factsheet at {url} did not return a PDF "
                f"(status={r.status_code}, content-type={ctype!r}, "
                f"{len(data)} bytes). The trustmf.com host fronts its "
                f"WordPress uploads with a WAF that serves a React SPA "
                f"shell to programmatic clients; an authenticated/browser "
                f"fetch is required."
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(out)
        return out

    # -------------------------------------------------------------------
    # PTR
    # -------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging

        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if "urnover" not in text and "PTR" not in text:
                    continue
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                m = _PTR_TEXT_RE.search(text)
                if not m:
                    continue
                try:
                    raw = float(m.group(1))
                except ValueError:
                    continue
                ptr = _normalise_ptr(raw, had_percent=bool(m.group(2)))
                if ptr is None:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr,
                    source_amc=self.amc_slug,
                )

    # parse_holdings: inherited default (holdings come from the separate
    # Excel path, per the brief).
