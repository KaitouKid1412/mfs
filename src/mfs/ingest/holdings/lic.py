"""LIC Mutual Fund monthly portfolio holdings adapter (Phase 5).

LIC publishes one SEBI-format Excel per scheme per month. Its
"Monthly / Fortnightly Portfolio" page
(https://www.licmf.com/downloads/monthly-portfolio) is a jQuery page whose
scheme/year/month dropdowns and the final download links are driven by a
chain of server-side AJAX POST endpoints (not a heavy JS SPA — the endpoints
return ready-to-parse HTML fragments):

  1. POST /downloads/portfolio-filter-options  {fund_category, filter=category}
        -> <option value='<SCHEME_CODE>'>Scheme Name</option> ...
  2. POST /downloads/portfolio-files
        {scheme_code, type=monthly_portfolio, month=<M>, year=<YYYY>}
        -> an HTML card containing
           <a href="/assets/downloads/portfolio/monthly/<YYYY>/<M>/
                    <CODE><DD-MM-YYYY-HH:MM:SS>.xlsx">

The fund-category list itself is the static <select class="fund_category">
on the portfolio page (Equity / Hybrid / ETFs & Index Funds / Debt /
Solution Oriented Funds); we scrape it rather than hardcode so a new LIC
category is picked up automatically.

The per-scheme Excel filename embeds an upload timestamp (e.g.
``LEEQTF07-05-2026-13:28:09.xlsx``), so the download URL is NOT
deterministic — we MUST resolve it per scheme via the portfolio-files
endpoint. ``month`` is the data month (April data -> month=4), NOT the May
publish month, even though the file is published in May.

TLS NOTE: www.licmf.com serves only the leaf certificate and omits the
``GeoTrust TLS RSA CA G1`` intermediate (chain depth 1), so a stock
httpx/certifi client fails verification with "unable to get local issuer
certificate" — even though curl succeeds (macOS pulls the intermediate from
the keychain / AIA). The repo's shared ``mfs.io.http`` client therefore can't
reach LIC. We build a private httpx client whose SSL context is certifi PLUS
the embedded GeoTrust intermediate (valid to Nov 2027), with a runtime AIA
fallback that fetches the live intermediate from the leaf's CA-Issuers URL if
the embedded one ever stops completing the chain. We never disable
verification.

Excel layout (validated against LIC MF Flexi Cap, April 2026):
- Sheet 0 (named after the scheme code, e.g. 'LEEQTF') holds the portfolio.
- Row 0: scheme banner. Row 2: "Monthly Portfolio Statement as on April...".
- Row 3: header — col B (1)='Name of the Instrument', col C (2)='ISIN',
  col D (3)='Industry / Rating', col E (4)='Quantity', col F (5)='Market/
  Fair Value...', col G (6)='Rounded, % to Net Assets', col H (7)='Yield'.
- Row 4+: section banners ('Equity & Equity related', '(a)Listed / Awaiting
  listing on Stock Exchanges', ...) interleaved with ISIN-bearing holding
  rows.

This is the standard SEBI layout: the shared ``parse_sebi_excel`` auto-detects
the ISIN/name/weight columns ('to net assets' resolves the weight column) and
the weight unit. LIC stores "% to Net Assets" as a FRACTION (ICICI Bank shows
0.0655 for 6.55%); the generic parser's total-magnitude rule detects that
(ISIN weights sum to ~1.0) and scales to percent automatically. So
``fetch_excel`` and ``parse_excel`` are inherited unchanged from
``GenericHoldingsAdapter`` (portfolio is on sheet 0).
"""

from __future__ import annotations

import re
import ssl
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urljoin

import certifi
import httpx

from mfs import paths
from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_HOST = "https://www.licmf.com"
_PORTFOLIO_PAGE = _HOST + "/downloads/monthly-portfolio"
_FILTER_EP = _HOST + "/downloads/portfolio-filter-options"
_FILES_EP = _HOST + "/downloads/portfolio-files"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)

# GeoTrust TLS RSA CA G1 — the intermediate LIC's server fails to send.
# Issued by DigiCert Global Root G2 (present in certifi). Valid to 2027-11-02.
# Embedding keeps discovery self-contained (no runtime CA fetch on the happy
# path); _ssl_context() falls back to fetching the live intermediate via the
# leaf's AIA URL if this ever stops completing the chain.
_GEOTRUST_INTERMEDIATE_PEM = """\
-----BEGIN CERTIFICATE-----
MIIEjTCCA3WgAwIBAgIQDQd4KhM/xvmlcpbhMf/ReTANBgkqhkiG9w0BAQsFADBh
MQswCQYDVQQGEwJVUzEVMBMGA1UEChMMRGlnaUNlcnQgSW5jMRkwFwYDVQQLExB3
d3cuZGlnaWNlcnQuY29tMSAwHgYDVQQDExdEaWdpQ2VydCBHbG9iYWwgUm9vdCBH
MjAeFw0xNzExMDIxMjIzMzdaFw0yNzExMDIxMjIzMzdaMGAxCzAJBgNVBAYTAlVT
MRUwEwYDVQQKEwxEaWdpQ2VydCBJbmMxGTAXBgNVBAsTEHd3dy5kaWdpY2VydC5j
b20xHzAdBgNVBAMTFkdlb1RydXN0IFRMUyBSU0EgQ0EgRzEwggEiMA0GCSqGSIb3
DQEBAQUAA4IBDwAwggEKAoIBAQC+F+jsvikKy/65LWEx/TMkCDIuWegh1Ngwvm4Q
yISgP7oU5d79eoySG3vOhC3w/3jEMuipoH1fBtp7m0tTpsYbAhch4XA7rfuD6whU
gajeErLVxoiWMPkC/DnUvbgi74BJmdBiuGHQSd7LwsuXpTEGG9fYXcbTVN5SATYq
DfbexbYxTMwVJWoVb6lrBEgM3gBBqiiAiy800xu1Nq07JdCIQkBsNpFtZbIZhsDS
fzlGWP4wEmBQ3O67c+ZXkFr2DcrXBEtHam80Gp2SNhou2U5U7UesDL/xgLK6/0d7
6TnEVMSUVJkZ8VeZr+IUIlvoLrtjLbqugb0T3OYXW+CQU0kBAgMBAAGjggFAMIIB
PDAdBgNVHQ4EFgQUlE/UXYvkpOKmgP792PkA76O+AlcwHwYDVR0jBBgwFoAUTiJU
IBiV5uNu5g/6+rkS7QYXjzkwDgYDVR0PAQH/BAQDAgGGMB0GA1UdJQQWMBQGCCsG
AQUFBwMBBggrBgEFBQcDAjASBgNVHRMBAf8ECDAGAQH/AgEAMDQGCCsGAQUFBwEB
BCgwJjAkBggrBgEFBQcwAYYYaHR0cDovL29jc3AuZGlnaWNlcnQuY29tMEIGA1Ud
HwQ7MDkwN6A1oDOGMWh0dHA6Ly9jcmwzLmRpZ2ljZXJ0LmNvbS9EaWdpQ2VydEds
b2JhbFJvb3RHMi5jcmwwPQYDVR0gBDYwNDAyBgRVHSAAMCowKAYIKwYBBQUHAgEW
HGh0dHBzOi8vd3d3LmRpZ2ljZXJ0LmNvbS9DUFMwDQYJKoZIhvcNAQELBQADggEB
AIIcBDqC6cWpyGUSXAjjAcYwsK4iiGF7KweG97i1RJz1kwZhRoo6orU1JtBYnjzB
c4+/sXmnHJk3mlPyL1xuIAt9sMeC7+vreRIF5wFBC0MCN5sbHwhNN1JzKbifNeP5
ozpZdQFmkCo+neBiKR6HqIA+LMTMCMMuv2khGGuPHmtDze4GmEGZtYLyF8EQpa5Y
jPuV6k2Cr/N3XxFpT3hRpt/3usU/Zb9wfKPtWpoznZ4/44c1p9rzFcZYrWkj3A+7
TNBJE0GmP2fhXhP1D/XVfIW/h0yCJGEiV9Glm/uGOa3DXHlmbAcxSyCRraG+ZBkA
7h4SeM6Y8l/7MBRpPCz6l8Y=
-----END CERTIFICATE-----
"""

# Leaf cert's AIA CA-Issuers URL — runtime fallback if the embedded
# intermediate ever stops completing the chain (e.g. LIC rotates issuers).
_AIA_INTERMEDIATE_URL = "http://cacerts.geotrust.com/GeoTrustTLSRSACAG1.crt"

# LIC's WAF throttles bursts of POSTs from one connection: after ~11 rapid
# portfolio-files calls it starts returning 403 to otherwise-valid requests.
# We pace requests and retry 403s with backoff so no scheme is dropped (a
# silent miss would violate the pipeline's fail-fast "no partial inputs"
# rule). A genuine "no file for this month" answer is still a 200 with an
# empty fragment, so 403 unambiguously means throttling, not absence.
_PACE_S = 0.4
_MAX_403_RETRIES = 6
_RETRY_BASE_S = 1.0

# Final download link inside the portfolio-files HTML fragment.
_FILE_HREF_RE = re.compile(
    r'href="(?P<url>/assets/downloads/portfolio/monthly/[^"]+?\.xlsx)"',
    re.IGNORECASE,
)

# Scheme-code <option> inside a portfolio-filter-options fragment. We drop the
# empty placeholder option (value='').
_OPTION_RE = re.compile(
    r"<option\s+value='(?P<code>[^']+)'>(?P<name>[^<]*)</option>",
    re.IGNORECASE,
)

# The static fund-category <select> on the portfolio page.
_CATEGORY_SELECT_RE = re.compile(
    r'class="fund_category"[^>]*>(?P<body>.*?)</select>', re.IGNORECASE | re.DOTALL
)

# LIC's portfolio-filter dropdown glues "Mid Cap" into one token for exactly
# one ranked scheme: it prints "LIC MF Large & Midcap Fund" while
# scheme_master records "LIC MF Large & Mid Cap Fund". Canonicalized, the
# glued "MIDCAP" token matches neither "MID" nor "CAP", so the orchestrator's
# token_set_ratio scorer mis-resolves the printed name to "LIC MF Large Cap
# Fund" (93.3) instead of the true target — leaving the Large & Mid Cap fund
# with no holdings. Splitting the glued token back to "Mid Cap" lifts the
# match to 100 and matches scheme_master exactly. The transform is anchored on
# word boundaries; LIC's other cap-class schemes already print "Mid cap" /
# "Small Cap" with a space, so they are untouched, and the only other glued
# occurrence ("Nifty Midcap 100 ETF") is an unranked ETF (canonical_category
# NULL) that was already below the match threshold either way.
_MIDCAP_GLUE_RE = re.compile(r"\bMidcap\b", re.IGNORECASE)


def _normalize_printed_name(name: str) -> str:
    """Repair LIC's glued ``Midcap`` token so the orchestrator's fuzzy
    matcher resolves the printed name to the correct scheme_master row."""
    return _MIDCAP_GLUE_RE.sub("Mid Cap", name)


def _ssl_context() -> ssl.SSLContext:
    """certifi trust store + the GeoTrust intermediate LIC fails to send.

    Tries the embedded PEM first; if loading it fails for any reason, fetches
    the live intermediate from the leaf cert's AIA URL (DER -> PEM). Never
    disables verification.
    """
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        ctx.load_verify_locations(cadata=_GEOTRUST_INTERMEDIATE_PEM)
        return ctx
    except ssl.SSLError:  # pragma: no cover — embedded PEM is known-good
        pass
    # Fallback: pull the intermediate from the CA-Issuers endpoint.
    der = httpx.get(_AIA_INTERMEDIATE_URL, timeout=30.0).content
    pem = ssl.DER_cert_to_PEM_cert(der)
    ctx.load_verify_locations(cadata=pem)
    return ctx


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=60.0,
        headers={
            "User-Agent": _UA,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
            "Referer": _PORTFOLIO_PAGE,
        },
        verify=_ssl_context(),
        follow_redirects=True,
    )


def _post_throttled(
    c: httpx.Client, url: str, data: dict[str, str]
) -> httpx.Response:
    """POST that paces requests and retries LIC's WAF 403s with backoff.

    Returns the first 200 response. Raises ``raise_for_status`` on a non-403
    error (a real failure we must surface), or on a 403 that survives all
    retries (so the caller fails loudly rather than dropping a scheme).
    """
    time.sleep(_PACE_S)
    last: httpx.Response | None = None
    for attempt in range(_MAX_403_RETRIES):
        r = c.post(url, data=data)
        if r.status_code != 403:
            r.raise_for_status()
            return r
        last = r
        time.sleep(_RETRY_BASE_S * (attempt + 1))
    # All retries exhausted on 403 — let raise_for_status throw.
    assert last is not None
    last.raise_for_status()
    return last  # unreachable; keeps the type checker happy


def _month_num(ym: str) -> str:
    """data_ym='2026-04' -> '4' (LIC keys portfolio-files by the data month,
    un-zero-padded, not the publish month)."""
    return str(int(ym.split("-")[1]))


def _year(ym: str) -> str:
    return ym.split("-")[0]


@register_adapter
class LicHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "lic"
    source_label = "LIC Mutual Fund"

    # Portfolio is on sheet 0; the generic SEBI parser handles columns + the
    # fraction-weight unit, so fetch_excel + parse_excel are inherited.
    sheet_index: int | None = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {printed_scheme_name: absolute .xlsx URL} for data month
        ``ym`` across every LIC fund category.

        For each category we list its scheme codes, then resolve each
        scheme's month-end Excel via the portfolio-files endpoint (the URL
        embeds an upload timestamp, so it can't be constructed). Schemes with
        no file for this month are silently skipped.
        """
        month = _month_num(ym)
        year = _year(ym)
        out: dict[str, str] = {}
        n_codes = 0
        with _client() as c:
            categories = self._fund_categories(c)
            codes = self._scheme_codes(c, categories)
            n_codes = len(codes)
            for code, name in codes.items():
                url = self._month_file_url(c, code, month, year)
                if url:
                    out.setdefault(name, url)
        log.info(
            "holdings.lic.discover",
            ym=ym, month=month, year=year,
            n_categories=len(categories), n_codes=n_codes, n_schemes=len(out),
        )
        return out

    def _fund_categories(self, c: httpx.Client) -> list[str]:
        """Scrape the fund-category <select> on the portfolio page."""
        html = c.get(_PORTFOLIO_PAGE).text
        m = _CATEGORY_SELECT_RE.search(html)
        if not m:
            log.warning("holdings.lic.no_category_select")
            return []
        cats = [
            v for v in re.findall(r"<option value=\"([^\"]*)\"", m.group("body"))
            if v.strip()
        ]
        # The page sometimes single-quotes option values; accept both.
        if not cats:
            cats = [
                v for v in re.findall(r"<option value='([^']*)'", m.group("body"))
                if v.strip()
            ]
        return cats

    def _scheme_codes(
        self, c: httpx.Client, categories: list[str]
    ) -> dict[str, str]:
        """Return {scheme_code: printed_name} merged across all categories."""
        out: dict[str, str] = {}
        for cat in categories:
            r = _post_throttled(
                c, _FILTER_EP, {"fund_category": cat, "filter": "category"}
            )
            for m in _OPTION_RE.finditer(r.text):
                code = m.group("code").strip()
                name = re.sub(r"\s+", " ", m.group("name")).strip()
                name = _normalize_printed_name(name)
                if not code or not name:
                    continue
                # Codes are unique; first category to list one wins.
                out.setdefault(code, name)
        return out

    def _month_file_url(
        self, c: httpx.Client, code: str, month: str, year: str
    ) -> str | None:
        """Resolve a scheme's monthly-portfolio .xlsx URL, or None if the
        scheme published no file for this month."""
        r = _post_throttled(
            c,
            _FILES_EP,
            {
                "scheme_code": code,
                "type": "monthly_portfolio",
                "month": month,
                "year": year,
            },
        )
        m = _FILE_HREF_RE.search(r.text)
        if not m:
            return None
        return urljoin(_HOST, m.group("url"))

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Download a scheme's Excel (cached) using LIC's TLS-fixed client.

        We override the inherited fetch (which uses the shared http client)
        because the shared client can't complete LIC's certificate chain.
        """
        out = paths.holdings_excel_raw(self.amc_slug, ym, scheme_filename)
        if out.exists():
            return out
        with _client() as c:
            resp = c.get(url)
            resp.raise_for_status()
            data = resp.content
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(out)
        return out

    def parse_excel(
        self, excel_path: Path, scheme_name_printed: str, ym: str
    ) -> Iterable[ParsedHoldingRecord]:
        # Inherited generic SEBI parse (sheet 0, auto column + unit detection).
        return super().parse_excel(excel_path, scheme_name_printed, ym)
