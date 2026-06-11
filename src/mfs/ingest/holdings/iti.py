"""ITI Mutual Fund monthly portfolio holdings adapter (Phase 5).

Unlike HDFC (one Excel per scheme), ITI publishes ONE consolidated workbook
per month covering every live ITI scheme — the same shape as Nippon. So
discovery enumerates schemes from the workbook's ``Index`` sheet and every
returned dict entry maps to that single master URL; ``parse_excel`` then
selects the one sheet for the requested scheme.

DISCOVERY (the hard part). The disclosures site (``www.itiamc.com/
statuory-disclosure``) is an Angular SPA — the static HTML carries no
``.xlsx`` links. The Angular bundle (``main.*.js``) revealed a JSON catalog
API at ``https://itiamc.com/jeeth/api/v1/catalog/`` whose every request body
and response body is AES-encrypted (the bundle has
``isEncryptionEnabled:!0``). The crypto, recovered verbatim from the
``encryptionProviderService`` in the bundle, is:

    CryptoJS.AES.encrypt(JSON.stringify(payload), key, {iv, mode:CBC,
                         padding:Pkcs7}).toString()   # default base64
  with
    key = CryptoJS.enc.Latin1.parse("aar6tzij8o1snaar")   # AES-128, 16 bytes
    iv  = CryptoJS.enc.Latin1.parse("0123456789ABCDEF")   # 16 bytes

The SPA wraps each POST body as ``{"eData": <base64 ciphertext>}`` (the
plaintext payload additionally carries a random 32-char ``guid`` and a
``timeStamp``, which the server ignores for reads but the SPA always sends),
and the response is ``{"status":0,"message":...,"data":{...}}`` likewise
returned inside ``{"eData": <ciphertext>}``. We replicate both halves with
``cryptography`` (AES-128-CBC + PKCS7).

The portfolio catalog lives under the ``getPartnerDocumentByType`` endpoint
with ``{"type":"Disclosure"}``: the decrypted ``data.typeList`` is a tree of
``subType`` → ``subTypesList`` (``topic`` → ``topicsList``). The node we want
is ``subType == "Portfolio Disclosures"`` / ``topic == "Monthly"``; each
``topicsList`` entry is::

    {"id":6391,
     "url":"https://itiamc.com/admin/pdf/...-Monthly_Portfolio_-_April_2026.xlsx",
     "fileName":"Monthly Portfolio - April 2026",   # the DATA month
     "month":"May","year":"2026"}                   # the PUBLISH month

CRITICAL month note: the row-level ``month``/``year`` are the PUBLISH month
(April-2026 data is published in May-2026). The DATA month is encoded in
``fileName`` ("Monthly Portfolio - <MonthName> <YYYY>"). So we parse the data
month out of ``fileName`` and keep the entry whose data month == ``ym`` — this
is parameterized by ``ym`` so next month works unchanged.

Per-scheme sheet layout (validated against ITI Flexi Cap, April 2026):
- ``Index`` sheet: col A = Sr No, col B = "Short Name" (== the per-scheme
  sheet name, e.g. "ITIFCF"), col C = full "Scheme Name".
- Sheets 1..N: one per scheme, sheet name == the short code.
- Per-scheme: a clean generic-SEBI table — header row with ``Name of the
  Instrument`` / ``ISIN`` / ``Industry`` / Quantity / Market Value / ``% to
  Net Assets``, section banners ("Equity & Equity related", "(a) Listed /
  awaiting listing on Stock Exchanges", ...) and ISIN-bearing holding rows.
- UNIT: like Nippon, ``% to Net Assets`` is stored as a **fraction** (0.0482
  = 4.82%). We do NOT special-case this: the shared ``parse_sebi_excel``
  auto-detects the fraction unit (total ISIN weight <= 1.5 ⇒ scale ×100), so
  ``parse_excel`` delegates to it with the resolved per-scheme sheet index.
"""

from __future__ import annotations

import base64
import json
import random
import re
import string
import time
from collections.abc import Iterable
from pathlib import Path

import httpx
import openpyxl
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from mfs import paths
from mfs.config import get_settings
from mfs.ingest.holdings._generic import GenericHoldingsAdapter, parse_sebi_excel
from mfs.ingest.holdings._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_API_BASE = "https://itiamc.com/jeeth/api/v1/catalog/"
_PARTNER_DOCS = _API_BASE + "getPartnerDocumentByType"

# AES-128-CBC key + IV, recovered verbatim from the Angular bundle's
# encryptionProviderService (CryptoJS .enc.Latin1.parse of these literals).
_AES_KEY = b"aar6tzij8o1snaar"
_AES_IV = b"0123456789ABCDEF"

_MONTHS_FULL = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

# Discovery-name normalization. The workbook's Index sheet spells the
# Large-and-Midcap scheme as "ITI Large & Mid Cap Fund" — "Mid Cap" as two
# words. The shared fuzzy matcher uses token_set_ratio, which treats the
# token set {large, cap} of "ITI Large Cap Fund" as a SUBSET of {large, mid,
# cap} and so scores BOTH printed names 100 against "ITI Large Cap Fund",
# collapsing the Large-&-Midcap sheet onto the Large-Cap scheme_code (and the
# rows then dedupe away). scheme_master spells the L&M scheme "Large & Midcap"
# (one token), which disambiguates cleanly. So we fold the two-word workbook
# spelling onto it at discovery time. CRITICAL: scope the fold to the FULL
# "Large & Mid Cap" phrase — the standalone scheme "ITI Mid Cap Fund" (code
# 148733) is spelled with two words in BOTH the workbook and scheme_master, so
# a blanket "Mid Cap"→"Midcap" would wrongly drag it onto the L&M code.
_LARGE_MIDCAP_RE = re.compile(r"\bLarge\s*&\s*Mid\s+Cap\b", re.IGNORECASE)


def _normalize_discovery_name(name: str) -> str:
    """Fold workbook scheme-name spellings onto scheme_master's so the shared
    fuzzy matcher resolves them uniquely (see _LARGE_MIDCAP_RE rationale)."""
    return _LARGE_MIDCAP_RE.sub("Large & Midcap", name)


# Data month token inside fileName, e.g. "Monthly Portfolio - April 2026".
# Tolerant of an optional space after the dash and an optional space before
# the year (ITI is inconsistent: "Monthly Portfolio - April 2026" vs
# "Monthly Portfolio -March 2026").
_FILENAME_MONTH_RE = re.compile(
    r"(?P<month>january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+(?P<year>\d{4})",
    re.IGNORECASE,
)

# A browser-ish header set for the encrypted JSON API + the CDN download.
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.itiamc.com",
    "Referer": "https://www.itiamc.com/",
}


def _encrypt(plaintext: str) -> str:
    """CryptoJS-compatible AES-128-CBC + PKCS7, base64 output."""
    p = padding.PKCS7(128).padder()
    data = p.update(plaintext.encode("utf-8")) + p.finalize()
    enc = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_IV)).encryptor()
    ct = enc.update(data) + enc.finalize()
    return base64.b64encode(ct).decode("ascii")


def _decrypt(b64: str) -> str:
    ct = base64.b64decode(b64)
    dec = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_IV)).decryptor()
    pt = dec.update(ct) + dec.finalize()
    u = padding.PKCS7(128).unpadder()
    return (u.update(pt) + u.finalize()).decode("utf-8")


def _guid() -> str:
    """32-char alnum guid, mirroring the SPA's generateGuid()."""
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(32))


def _post_catalog(endpoint: str, payload: dict) -> dict:
    """POST an encrypted body to the catalog API, return the decrypted JSON.

    The SPA always attaches a guid + timeStamp to the plaintext payload; the
    server ignores them for reads, but we send them for fidelity.
    """
    body = dict(payload)
    body["guid"] = _guid()
    body["timeStamp"] = int(time.time() * 1000)
    enc_body = {"eData": _encrypt(json.dumps(body))}
    s = get_settings()
    with httpx.Client(
        timeout=s.http_timeout, headers=_HEADERS, follow_redirects=True
    ) as c:
        r = c.post(endpoint, json=enc_body)
        r.raise_for_status()
        j = r.json()
    if isinstance(j, dict) and j.get("eData"):
        return json.loads(_decrypt(j["eData"]))
    # Defensive: some error paths may come back un-encrypted.
    return j if isinstance(j, dict) else {}


def _filename_ym(file_name: str) -> str | None:
    """Reverse-derive data_ym (YYYY-MM) from a Monthly-Portfolio fileName.

    Returns None if no recognizable ``<Month> <Year>`` token is present.
    """
    if not file_name:
        return None
    m = _FILENAME_MONTH_RE.search(file_name)
    if not m:
        return None
    month = _MONTHS_FULL.index(m.group("month").lower()) + 1
    return f"{int(m.group('year')):04d}-{month:02d}"


def _find_monthly_url(ym: str) -> str | None:
    """Hit the encrypted catalog API and return the consolidated monthly
    workbook URL whose fileName data month == ``ym`` (None if absent)."""
    res = _post_catalog(_PARTNER_DOCS, {"type": "Disclosure"})
    type_list = ((res.get("data") or {}).get("typeList")) or []
    for item in type_list:
        if (item or {}).get("subType") != "Portfolio Disclosures":
            continue
        for st in item.get("subTypesList") or []:
            if (st or {}).get("topic") != "Monthly":
                continue
            for doc in st.get("topicsList") or []:
                url = (doc or {}).get("url") or ""
                if not url.lower().endswith((".xlsx", ".xls")):
                    continue
                if _filename_ym(doc.get("fileName") or "") == ym:
                    return url
    return None


def _master_excel_filename(ym: str) -> str:
    """Canonical local-cache filename for the consolidated monthly Excel."""
    return f"ITI-MONTHLY-PORTFOLIO-{ym}.xlsx"


@register_adapter
class ItiHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "iti"
    source_label = "ITI Mutual Fund"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Find the consolidated monthly workbook for ``ym``, cache it once,
        and enumerate its per-scheme entries from the Index sheet.

        Every returned entry maps to the SAME master URL (ITI ships one
        workbook for all schemes); ``fetch_excel`` short-circuits to the
        cached file and ``parse_excel`` resolves the per-scheme sheet.
        """
        master_url = _find_monthly_url(ym)
        if master_url is None:
            log.warning("holdings.iti.no_url_for_ym", ym=ym)
            return {}

        master_path = self._cached_master_path(ym)
        if not master_path.exists():
            self._download_master(master_url, master_path)

        out: dict[str, str] = {}
        try:
            wb = openpyxl.load_workbook(
                master_path, read_only=True, data_only=True
            )
        except Exception as e:  # noqa: BLE001 — surface any payload issue
            log.error(
                "holdings.iti.workbook_open_failed",
                ym=ym, path=str(master_path), err=str(e),
            )
            return {}
        try:
            if "Index" not in wb.sheetnames:
                log.warning(
                    "holdings.iti.no_index_sheet",
                    ym=ym, sheetnames=wb.sheetnames[:5],
                )
                return {}
            sheet_set = set(wb.sheetnames)
            for i, row in enumerate(wb["Index"].iter_rows(values_only=True)):
                if i == 0:
                    continue  # header: Sr No. | Short Name | Scheme Name
                code = row[1] if len(row) > 1 else None
                name = row[2] if len(row) > 2 else None
                if not code or not isinstance(name, str):
                    continue
                code_s = str(code).strip()
                if code_s not in sheet_set:
                    # Index row points at a non-existent sheet — ITI-side
                    # hiccup; skip rather than fail.
                    continue
                scheme = _normalize_discovery_name(
                    re.sub(r"\s+", " ", name).strip()
                )
                if scheme:
                    out.setdefault(scheme, master_url)
        finally:
            wb.close()

        log.info(
            "holdings.iti.discover",
            ym=ym, n_schemes=len(out), master_url=master_url,
        )
        return out

    # ------------------------------------------------------------------
    # Download (one shared workbook for all schemes)
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Return the cached master workbook for this month.

        ITI's master Excel is shared across all schemes, so
        ``scheme_filename`` is intentionally ignored; we download once.
        """
        master_path = self._cached_master_path(ym)
        if master_path.exists():
            return master_path
        return self._download_master(url, master_path)

    def _cached_master_path(self, ym: str) -> Path:
        return paths.holdings_excel_raw(
            self.amc_slug, ym, _master_excel_filename(ym)
        )

    @staticmethod
    def _download_master(url: str, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        s = get_settings()
        with httpx.Client(
            timeout=max(s.http_timeout, 120),
            headers={"User-Agent": s.user_agent, "Referer": "https://www.itiamc.com/"},
            follow_redirects=True,
        ) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.content
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(out_path)
        return out_path

    # ------------------------------------------------------------------
    # Parse (select the scheme's sheet from the consolidated workbook)
    # ------------------------------------------------------------------

    def parse_excel(
        self,
        excel_path: Path,
        scheme_name_printed: str,
        ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        """Resolve the per-scheme sheet from the Index, then delegate to the
        shared generic SEBI parser (which auto-detects the fraction unit)."""
        sheet_index = self._resolve_sheet_index(excel_path, scheme_name_printed)
        if sheet_index is None:
            log.warning(
                "holdings.iti.scheme_not_in_index",
                scheme=scheme_name_printed, ym=ym,
            )
            return iter(())
        return parse_sebi_excel(
            excel_path, scheme_name_printed, self.amc_slug,
            sheet_index=sheet_index,
        )

    @staticmethod
    def _resolve_sheet_index(
        excel_path: Path, scheme_name_printed: str
    ) -> int | None:
        """Map a printed scheme name to its 0-based sheet index via the
        Index sheet (col B short-code → workbook sheet position)."""
        wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        try:
            if "Index" not in wb.sheetnames:
                return None
            target = re.sub(r"\s+", " ", scheme_name_printed.strip()).lower()
            sheetnames = wb.sheetnames
            for i, row in enumerate(wb["Index"].iter_rows(values_only=True)):
                if i == 0:
                    continue
                code = row[1] if len(row) > 1 else None
                name = row[2] if len(row) > 2 else None
                if not code or not isinstance(name, str):
                    continue
                # Normalize the Index name the same way discovery does so the
                # normalized printed key round-trips back to its raw sheet.
                index_name = _normalize_discovery_name(
                    re.sub(r"\s+", " ", name.strip())
                )
                if index_name.lower() != target:
                    continue
                code_s = str(code).strip()
                if code_s in sheetnames:
                    return sheetnames.index(code_s)
            return None
        finally:
            wb.close()
