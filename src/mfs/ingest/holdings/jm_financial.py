"""JM Financial Mutual Fund monthly portfolio holdings adapter (Phase 5).

JM Financial publishes one SEBI-format Excel per scheme per month — the ideal
shape for ``GenericHoldingsAdapter`` + the shared ``parse_sebi_excel`` (sheet 0
carries the equity table in the canonical ISIN / Name of Instrument /
% To Net Assets layout, fraction units).

Discovery is the bespoke part. The downloads page
(https://www.jmfinancialmf.com/downloads/Portfolio-Disclosure) is a
Create-React-App SPA whose server-rendered HTML carries NO .xlsx links; the
listing is hydrated client-side from a JSON API on a separate host:

    POST https://jmmfapi.jmfinancialmf.com/api/GetDownloadDrop
        body {"IICategoryID":"0"}            -> category list (encrypted)
        body {"IICategoryID":"2"}            -> subcategories of Portfolio
                                                Disclosure (encrypted)
    POST https://jmmfapi.jmfinancialmf.com/api/GetDownloadNew
        body {"IICategoryID":"2","IISubCategoryID":"4","IVsearch":""}
                                             -> the full download catalogue
                                                for "Monthly Portfolio of
                                                Schemes" (encrypted)

The category id for "Portfolio Disclosure" is 2 and the subcategory id for
"Monthly Portfolio of Schemes" is 4 (resolved dynamically from
``GetDownloadDrop`` rather than hardcoded, so a backend re-keying surfaces as
an explicit error instead of a silent miss).

RESPONSE ENCRYPTION: every API response is ``{"data": "<base64>"}`` where the
base64 decodes to AES-256-CBC ciphertext (PKCS7-padded). The SPA bundle ships
the symmetric key/IV in cleartext as ``REACT_APP_AES_KEY`` /
``REACT_APP_AES_IV`` (this is an obfuscation layer, not real auth — no login,
cookie, or token is involved), so we mirror its ``decrypt`` helper exactly.
The plaintext is a JSON array of records:

    {"DownloadID": 13921, "CategoryID": 2, "SubCategoryID": 4,
     "DocumentDate": "2026-05-08T00:00:00",
     "Title": "Monthly Portfolio - JM Flexicap Fund - April 30, 2026",
     "FileName": "CMS/downloads/Portfolio Disclosure/Monthly Portfolio of
                  Schemes/Monthly Portfolio - JM Flexicap Fund - April 30,
                  2026.xlsx",
     "FileEXT": ".xlsx"}

The file is served (URL-encoded ``FileName``) from the SPA origin
``https://www.jmfinancialmf.com/``.

DATE / PUBLISH OFFSET: the ``Title`` date tokens are inconsistent across
schemes ("Apr 30, 2026", "April 30 2026", "April 2026", "April 30 , 2026"),
so they are NOT a reliable month filter. The ``DocumentDate`` is the PUBLISH
date and is uniform across a batch — April-2026 data is published 2026-05-08,
the same +1-month offset as HDFC/SBI/Bandhan. We therefore key discovery off
``DocumentDate`` falling in the PUBLISH month (data month + 1) and recover the
printed scheme name by stripping the "Monthly Portfolio" prefix and the
trailing date token from the ``Title``.

Per-scheme Excel layout (validated against JM Flexicap Fund, April 2026):
- Single sheet (named after the scheme, e.g. 'JM Flexicap Fund').
- Row 0: AMC banner. Row 1: scheme name. Row 2: "Monthly Portfolio Statement
  for ...".
- Row 3: header — col A 'ISIN', col B 'Name of Instrument', col C
  'Rating/Industry', col D 'Quantity', col E 'Market Value (In Rs. lakh)',
  col F '% To Net Assets', then Maturity/Yield columns.
- Row 4+: section banners ('EQUITY & EQUITY RELATED', '(a) Listed / awaiting
  listing on Stock Exchanges', Debt/Money-Market/Cash banners on hybrids)
  interleaved with ISIN-bearing holding rows.

Because the per-scheme sheet IS the canonical SEBI layout, we inherit
``GenericHoldingsAdapter`` wholesale: ``parse_sebi_excel`` auto-detects the
header row, the ISIN/name/weight columns, and the weight unit (JM stores
'% To Net Assets' as a FRACTION, e.g. 0.0356 == 3.56%, which the parser's
sum-based detector scales to percent). ``fetch_excel`` does the standard
cached download. Validated on JM Flexicap: 80 equity ISIN rows summing to
~99.4%.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_API_BASE = "https://jmmfapi.jmfinancialmf.com/api/"
_FILE_HOST = "https://www.jmfinancialmf.com/"

# Symmetric key + IV shipped in cleartext in the SPA bundle (REACT_APP_AES_KEY
# / REACT_APP_AES_IV). This is response obfuscation, not authentication — the
# endpoints are anonymous. AES-256-CBC (32-byte key) with PKCS7 padding.
_AES_KEY = b"6fa979f20126cb08aa645a8f495f6d85"
_AES_IV = b"I8zyA4lVhMCaJ5Kg"

# The download-tree categories we drill into. Resolved dynamically by name so a
# backend re-keying of the numeric ids surfaces loudly rather than silently.
_CATEGORY_NAME = "Portfolio Disclosure"
_SUBCATEGORY_NAME = "Monthly Portfolio of Schemes"

_GET_DROP = "GetDownloadDrop"
_GET_NEW = "GetDownloadNew"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

# Strip the leading "Monthly Portfolio" disclosure-type prefix from a Title.
# Titles vary: "Monthly Portfolio - JM ...", "Monthly Portfolio- JM ...",
# "Monthly  Portfolio - JM ..." (double space). We normalize all of them.
_TITLE_PREFIX_RE = re.compile(
    r"^Monthly\s+Portfolio\s*[-:]?\s*", re.IGNORECASE
)

# Drop the trailing data-date token. Across schemes JM prints this as
# "April 30, 2026", "Apr 30, 2026", "April 30 2026", "April 30 , 2026",
# or just "April 2026" — so match an optional day with flexible punctuation.
_TITLE_DATE_RE = re.compile(
    r"\s*[-–]?\s*[A-Za-z]{3,9}\.?\s+(?:\d{1,2}\s*,?\s*)?\d{4}\s*$",
)


def _publish_ym(data_ym: str) -> str:
    """Data month -> publish month (publish = data + 1).

    JM tags each disclosure record with a ``DocumentDate`` that is the PUBLISH
    date; April-2026 data is published in May-2026. The Title's own date token
    is too inconsistent to filter on, so we key off the uniform publish month.
    """
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{m + 1:02d}"


def _decrypt(payload: str) -> object:
    """Decrypt one API response's base64 AES-256-CBC ``data`` blob to JSON."""
    ct = base64.b64decode(payload)
    cipher = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_IV))
    dec = cipher.decryptor()
    pt = dec.update(ct) + dec.finalize()
    pt = pt[: -pt[-1]]  # strip PKCS7 padding
    return json.loads(pt.decode("utf-8", errors="replace"))


def _scheme_name_from_title(title: str) -> str:
    """Recover the printed scheme name: strip the type prefix + trailing date.

    JM writes the Large & Mid Cap fund as 'JM Large and Midcap Fund' in the
    Title, but scheme_master canonicalizes it 'JM Large & Mid Cap Fund'; that
    phrasing gap drops the fuzzy match below threshold. We normalize the
    'and Midcap' phrasing to '& Mid Cap' so it matches cleanly. (The standalone
    'JM Midcap Fund' already matches at 100 and is left untouched.)
    """
    s = _TITLE_PREFIX_RE.sub("", title).strip()
    s = _TITLE_DATE_RE.sub("", s).strip().rstrip("-–").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\bLarge\s+and\s+Midcap\b", "Large & Mid Cap", s,
               flags=re.IGNORECASE)
    return s


@register_adapter
class JmFinancialHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "jm_financial"
    source_label = "JM Financial Mutual Fund"
    # Per-scheme files put the portfolio on sheet 0 in standard SEBI layout.
    sheet_index = 0

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {printed_scheme_name: absolute .xlsx URL} for data month
        ``ym`` by walking JM's encrypted downloads JSON API and keeping the
        Monthly Portfolio records whose publish ``DocumentDate`` falls in the
        publish month (data month + 1)."""
        publish_ym = _publish_ym(ym)
        with httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={
                "User-Agent": _UA,
                "Content-Type": "application/json",
                "Origin": "https://www.jmfinancialmf.com",
                "Referer": "https://www.jmfinancialmf.com/",
                "Accept": "application/json, text/plain, */*",
            },
        ) as client:
            cat_id, sub_id = self._resolve_ids(client)
            records = self._post(
                client,
                _GET_NEW,
                {
                    "IICategoryID": str(cat_id),
                    "IISubCategoryID": str(sub_id),
                    "IVsearch": "",
                },
            )

        if not isinstance(records, list):
            raise ValueError(
                "JM GetDownloadNew did not return a JSON array of records"
            )

        out: dict[str, str] = {}
        for rec in records:
            if not isinstance(rec, dict):
                continue
            if _record_publish_ym(rec.get("DocumentDate")) != publish_ym:
                continue
            file_name = rec.get("FileName")
            title = rec.get("Title")
            if not isinstance(file_name, str) or not isinstance(title, str):
                continue
            if not file_name.lower().endswith((".xlsx", ".xls")):
                continue
            name = _scheme_name_from_title(title)
            if not name:
                continue
            # URL-encode the path segments while preserving the slashes.
            url = _FILE_HOST + quote(file_name.lstrip("/"))
            out.setdefault(name, url)

        log.info(
            "holdings.jm_financial.discover",
            ym=ym, publish_ym=publish_ym, n_schemes=len(out),
        )
        return out

    def _resolve_ids(self, client: httpx.Client) -> tuple[int, int]:
        """Resolve (Portfolio-Disclosure category id, Monthly-Portfolio
        subcategory id) by name from the downloads category tree."""
        cats = self._post(client, _GET_DROP, {"IICategoryID": "0"})
        cat_id = _find_id(
            cats, "CategoryName", _CATEGORY_NAME, "DownloadCategoryID"
        )
        if cat_id is None:
            raise ValueError(
                f"JM downloads tree has no {_CATEGORY_NAME!r} category"
            )
        subs = self._post(client, _GET_DROP, {"IICategoryID": str(cat_id)})
        sub_id = _find_id(
            subs, "SubCategoryName", _SUBCATEGORY_NAME, "DownloadSubCategoryID"
        )
        if sub_id is None:
            raise ValueError(
                f"JM {_CATEGORY_NAME!r} has no {_SUBCATEGORY_NAME!r} "
                "subcategory"
            )
        return cat_id, sub_id

    @staticmethod
    def _post(client: httpx.Client, endpoint: str, body: dict) -> object:
        """POST a JSON body and decrypt the response's ``data`` blob."""
        r = client.post(_API_BASE + endpoint, json=body)
        r.raise_for_status()
        obj = r.json()
        data = obj.get("data") if isinstance(obj, dict) else None
        if not data:
            raise ValueError(
                f"JM {endpoint} returned no 'data' for body={body}"
            )
        return _decrypt(data)


def _find_id(
    records: object, name_key: str, name_val: str, id_key: str,
) -> int | None:
    """Find ``id_key`` in the record whose ``name_key`` equals ``name_val``
    (case-insensitive). Returns None if absent."""
    if not isinstance(records, list):
        return None
    target = name_val.strip().lower()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        nm = rec.get(name_key)
        if isinstance(nm, str) and nm.strip().lower() == target:
            rid = rec.get(id_key)
            if rid is not None:
                return int(rid)
    return None


def _record_publish_ym(document_date: object) -> str | None:
    """Extract YYYY-MM from a record's ISO ``DocumentDate`` (publish date)."""
    if not isinstance(document_date, str):
        return None
    try:
        d = date.fromisoformat(document_date[:10])
    except ValueError:
        return None
    return f"{d.year:04d}-{d.month:02d}"
