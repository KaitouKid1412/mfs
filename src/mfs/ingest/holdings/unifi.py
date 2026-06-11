"""Unifi Mutual Fund monthly portfolio holdings adapter (Phase 5).

Unifi is a new AMC (Unifi Asset Management, the MF arm of Unifi Capital).
It publishes its SEBI monthly portfolio statements as a STATIC list of
direct Excel links on its statutory-documents page:

    https://unifimf.com/statutorydocuments/

The page server-renders every disclosure URL as a plain <a href> (it is a
WordPress site; files live under /wp-content/uploads/fund-sheets/), so a
single page scrape gives the full month's catalog — no SPA / JSON API.

Per-scheme monthly-portfolio files use the ``MP-`` prefix and are named by
the AS-ON (month-end) date in ``DDMMYYYY``::

    MP-Unifi-Flexi-Cap-Fund-30042026.xlsx
    MP-Unifi-Dynamic-Asset-Allocation-Fund-30042026.xlsx
    MP-Unifi-Liquid-Fund-30042026.xls

So the file date IS the data month-end (no publish-month offset). We DON'T
confuse these with the ``FN-`` fortnightly files (Liquid Fund only) or the
``Monthly-Portfolio-*`` / ``Scheme_Performance_*`` / ``Scheme-Dashboard_*``
files, which are different artifacts. The scheme name is the hyphen-joined
segment between ``MP-`` and the trailing date (we restore spaces).

Excel layout (validated against Unifi Flexi Cap + Dynamic Asset Allocation,
April 2026):
- Sheet 0 (named after the scheme) holds the portfolio; the Flexi Cap book
  carries a second 'Missing ISIN' sheet that ``sheet_index=0`` skips.
- Row 0-2: AMC / scheme banner + "Monthly Portfolio Statement as on ...".
- Row 3: header — Name of Instrument | ISIN | Rating/Industry | Quantity |
  Market Value (In Rs. lakh) | % To Net Assets | YTM | YTC.
- Row 4+: section banners ('Equity & Equity related', 'a) Listed / awaiting
  listing on Stock Exchanges', Debt / Money-Market banners on the hybrid)
  interleaved with holding rows.

CRITICAL UNIT NOTE: Unifi stores '% To Net Assets' as a FRACTION (Bharti
Airtel at 0.0512 == 5.12%). The shared ``parse_sebi_excel`` auto-detects
the fraction vs percent unit from the portfolio's total weight magnitude
and scales fractions up by 100, so no bespoke parser is needed.

TLS QUIRK: ``unifimf.com`` (GoDaddy-issued ``*.unifimf.com`` leaf) does NOT
send its intermediate CA in the handshake, so the shared
``mfs.io.http`` client — which verifies strictly against certifi — fails
with "unable to get local issuer certificate". The leaf's root ("Go Daddy
Root Certificate Authority - G2") IS in certifi; only the intermediate ("Go
Daddy Secure Certificate Authority - G2") is missing. We therefore build a
private SSL context from certifi PLUS that intermediate (embedded below,
valid until 2031-05-03) and use it for both discovery and download. This
keeps FULL chain verification intact — it does not disable TLS checks — it
only supplies the one cert the server omits. We can't reuse
``discover_xlsx_links`` / the inherited ``fetch_excel`` because both route
through the strict shared client, so this adapter implements its own fetch
against the patched context.
"""

from __future__ import annotations

import calendar
import re
import ssl
from pathlib import Path
from urllib.parse import unquote

import certifi
import httpx

from mfs import paths
from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = "https://unifimf.com/statutorydocuments/"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

# GoDaddy intermediate the server omits from its handshake. Public CA cert,
# subject "Go Daddy Secure Certificate Authority - G2", chains to the
# certifi-trusted "Go Daddy Root Certificate Authority - G2"; valid until
# 2031-05-03. Embedding it (rather than fetching the AIA caIssuers URL at
# runtime) keeps discovery self-contained and offline-deterministic while
# preserving full chain verification.
_GODADDY_G2_INTERMEDIATE_PEM = """\
-----BEGIN CERTIFICATE-----
MIIE0DCCA7igAwIBAgIBBzANBgkqhkiG9w0BAQsFADCBgzELMAkGA1UEBhMCVVMx
EDAOBgNVBAgTB0FyaXpvbmExEzARBgNVBAcTClNjb3R0c2RhbGUxGjAYBgNVBAoT
EUdvRGFkZHkuY29tLCBJbmMuMTEwLwYDVQQDEyhHbyBEYWRkeSBSb290IENlcnRp
ZmljYXRlIEF1dGhvcml0eSAtIEcyMB4XDTExMDUwMzA3MDAwMFoXDTMxMDUwMzA3
MDAwMFowgbQxCzAJBgNVBAYTAlVTMRAwDgYDVQQIEwdBcml6b25hMRMwEQYDVQQH
EwpTY290dHNkYWxlMRowGAYDVQQKExFHb0RhZGR5LmNvbSwgSW5jLjEtMCsGA1UE
CxMkaHR0cDovL2NlcnRzLmdvZGFkZHkuY29tL3JlcG9zaXRvcnkvMTMwMQYDVQQD
EypHbyBEYWRkeSBTZWN1cmUgQ2VydGlmaWNhdGUgQXV0aG9yaXR5IC0gRzIwggEi
MA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQC54MsQ1K92vdSTYuswZLiBCGzD
BNliF44v/z5lz4/OYuY8UhzaFkVLVat4a2ODYpDOD2lsmcgaFItMzEUz6ojcnqOv
K/6AYZ15V8TPLvQ/MDxdR/yaFrzDN5ZBUY4RS1T4KL7QjL7wMDge87Am+GZHY23e
cSZHjzhHU9FGHbTj3ADqRay9vHHZqm8A29vNMDp5T19MR/gd71vCxJ1gO7GyQ5HY
pDNO6rPWJ0+tJYqlxvTV0KaudAVkV4i1RFXULSo6Pvi4vekyCgKUZMQWOlDxSq7n
eTOvDCAHf+jfBDnCaQJsY1L6d8EbyHSHyLmTGFBUNUtpTrw700kuH9zB0lL7AgMB
AAGjggEaMIIBFjAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBBjAdBgNV
HQ4EFgQUQMK9J47MNIMwojPX+2yz8LQsgM4wHwYDVR0jBBgwFoAUOpqFBxBnKLbv
9r0FQW4gwZTaD94wNAYIKwYBBQUHAQEEKDAmMCQGCCsGAQUFBzABhhhodHRwOi8v
b2NzcC5nb2RhZGR5LmNvbS8wNQYDVR0fBC4wLDAqoCigJoYkaHR0cDovL2NybC5n
b2RhZGR5LmNvbS9nZHJvb3QtZzIuY3JsMEYGA1UdIAQ/MD0wOwYEVR0gADAzMDEG
CCsGAQUFBwIBFiVodHRwczovL2NlcnRzLmdvZGFkZHkuY29tL3JlcG9zaXRvcnkv
MA0GCSqGSIb3DQEBCwUAA4IBAQAIfmyTEMg4uJapkEv/oV9PBO9sPpyIBslQj6Zz
91cxG7685C/b+LrTW+C05+Z5Yg4MotdqY3MxtfWoSKQ7CC2iXZDXtHwlTxFWMMS2
RJ17LJ3lXubvDGGqv+QqG+6EnriDfcFDzkSnE3ANkR/0yBOtg2DZ2HKocyQetawi
DsoXiWJYRBuriSUBAA/NxBti21G00w9RKpv0vHP8ds42pM3Z2Czqrpv1KrKQ0U11
GIo/ikGQI31bS/6kA1ibRrLDYGCD+H1QQc7CoZDDu+8CL9IVVO5EFdkKrqeKM+2x
LXY2JtwE65/3YR8V3Idv7kaWKK2hJn0KCacuBKONvPi8BDAB
-----END CERTIFICATE-----
"""

# Per-scheme monthly-portfolio Excel URL. The ``MP-`` prefix distinguishes
# the monthly portfolio from the ``FN-`` fortnightly Liquid files; the date
# segment is the data month-end in DDMMYYYY. We capture the full URL and
# parse the filename downstream.
_URL_RE = re.compile(
    r'(https://unifimf\.com/wp-content/uploads/fund-sheets/'
    r'MP-Unifi-[^"\s]+?-\d{8}\.xlsx?)'
)

# Filename -> (scheme name, DDMMYYYY date). Scheme name is the hyphen-joined
# body between "MP-" and the date; we restore spaces.
_FILENAME_RE = re.compile(
    r"^MP-(?P<scheme>.+?)-(?P<date>\d{8})\.xlsx?$",
    re.IGNORECASE,
)


def _data_month_end_ddmmyyyy(ym: str) -> str:
    """'2026-04' -> '30042026' (the as-on date Unifi prints in the filename).

    Uses the real last day of the month so 28-Feb / 30-Apr / 31-Mar all
    resolve correctly.
    """
    y, m = map(int, ym.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    return f"{last_day:02d}{m:02d}{y:04d}"


def _ssl_context() -> ssl.SSLContext:
    """certifi trust store + the GoDaddy intermediate the server omits."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cadata=_GODADDY_G2_INTERMEDIATE_PEM)
    return ctx


@register_adapter
class UnifiHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "unifi"
    source_label = "Unifi Mutual Fund"

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Scrape the statutory-documents page, return
        {printed_scheme_name: excel_url} for the requested data month.

        Matches only ``MP-Unifi-*-<DDMMYYYY>.xls[x]`` files whose embedded
        as-on date equals the requested month-end.
        """
        target_date = _data_month_end_ddmmyyyy(ym)
        with httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": _UA},
            verify=_ssl_context(),
        ) as c:
            r = c.get(_DISCLOSURE_PAGE)
            r.raise_for_status()
            html = r.text

        out: dict[str, str] = {}
        for mt in _URL_RE.finditer(html):
            url = mt.group(1)
            filename = unquote(url.rsplit("/", 1)[-1].split("?", 1)[0])
            fm = _FILENAME_RE.match(filename)
            if not fm or fm.group("date") != target_date:
                continue
            scheme = re.sub(r"\s+", " ", fm.group("scheme").replace("-", " ")).strip()
            if scheme:
                out.setdefault(scheme, url)
        log.info(
            "holdings.unifi.discover",
            ym=ym, target_date=target_date, n_schemes=len(out),
        )
        return out

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Download (cached) a single scheme's Excel over the patched-TLS
        client (the shared ``download_to`` verifies strictly and would fail
        on Unifi's incomplete cert chain)."""
        out = paths.holdings_excel_raw(self.amc_slug, ym, scheme_filename)
        if out.exists():
            return out
        with httpx.Client(
            timeout=120.0,
            follow_redirects=True,
            headers={"User-Agent": _UA},
            verify=_ssl_context(),
        ) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.content
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(out)
        return out
