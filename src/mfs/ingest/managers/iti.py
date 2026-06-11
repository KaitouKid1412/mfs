"""ITI Mutual Fund — factsheet adapter (Phase 3.D).

Calibrated against the April 2026 combined factsheet at
``data/raw/factsheets/iti/2026-04.pdf`` (~3.09 MB, 41 pages, 20 scheme
detail pages plus cover / TOC / market commentary / SIP returns / IDCW
history / riskometer / glossary).

URL discovery
-------------
ITI Mutual Fund's site is at ``https://itiamc.com`` (an Angular SPA;
``www.itimf.com`` is a squatted Vietnamese gambling page and
``www.itimutualfund.com`` is parked on GoDaddy / Afternic). The SPA
talks to a backend at ``/jeeth/api/v1/catalog/<endpoint>`` with
AES-128-CBC encrypted JSON payloads (key/IV hard-coded in the
``main.<hash>.js`` bundle). Two endpoints expose the factsheet manifest:

* ``digitalfactsheet`` — currently returns ``digitalFactsheetReturnValue: []``
  for every year filter on the 2026-04 manifest (empty backend table).
* ``getDocumentsByType`` with ``{"type": "downloads"}`` — returns
  ``documentList`` grouped by topic; the ``Factsheet`` topic carries one
  entry per month with a stable ``url`` shape:

      https://itiamc.com/admin/pdf/<upload_epoch_seconds>-<filename>.pdf

  e.g. April 2026 →
  ``https://itiamc.com/admin/pdf/1779445747-ITI_Factsheet_April_26.pdf``.

The ``<upload_epoch_seconds>`` prefix and the exact filename casing
(``Factsheet`` vs ``FACTSHEET``, two-digit year ``April_26`` vs four-digit
``April_2026``) are NOT deterministic across months — they reflect the
upload timestamp and whoever uploaded it. We therefore CANNOT construct
the URL from ym alone; ``build_url(ym)`` performs a live API lookup,
matching the manifest's ``fileName`` field (``Factsheet - April 2026``) to
the data month. The resolver is encapsulated in
``_resolve_pdf_url(ym)`` so tests can patch the network leg.

Layout findings driving the parser
----------------------------------
* Pages 1-9 are cover / TOC / market commentary / equity-hybrid-debt
  Ready Reckoners. Pages 10-29 are the 20 scheme detail pages
  (11 equity + 1 sector + 2 thematic + 2 hybrid + 5 debt). Pages 30+
  are SIP returns / dividend history / riskometer / disclaimers.

* Each scheme detail page carries the markers ``CATEGORY OF SCHEME`` and
  ``PORTFOLIO DETAILS`` — used as the page gate (cheap and exclusive).
  The Ready Reckoner pages have neither marker. SIP / IDCW history /
  riskometer pages also don't carry both markers.

* Scheme name lives on a line beginning with ``ITI ``. On most pages
  it's the first non-empty line, but two pages (``ITI Large & Mid Cap
  Fund`` on PDF page 20, ``ITI Pharma and Healthcare Fund`` on page 16)
  have a chart-derived banner ahead of the title. We scan the first 60
  non-empty lines for an ``ITI ... Fund`` shape.

* PTR is printed as ``Portfolio Turnover Ratio 1.08`` — a plain
  fraction in the same convention as LIC / UTI ("times"). Storage is
  also a fraction, so PASS-THROUGH (no /100 divide). On newly-launched
  schemes (< 1 year old, e.g. ITI Bharat Consumption Fund, ITI Business
  Cycle Fund) the value is printed as ``-`` or ``NA`` with a footnote
  explaining the omission — we drop those rows.

PTR storage convention
----------------------
ITI prints PTR as a FRACTION (1.08 = 108% turnover). Storage convention
is the same fraction, so the adapter passes the value through unchanged.

Calibration counts on 2026-04
-----------------------------
* 20 scheme detail pages detected (pages 10-29)
* 12 PTR rows extracted (11 equity + 1 hybrid Balanced Advantage; the
  remaining 8 schemes legitimately omit PTR — 5 debt schemes, 1
  arbitrage, and the 2 newly-launched Bharat Consumption / Business
  Cycle funds)
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path

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


# ---------------------------------------------------------------------------
# URL resolver — talks to the ITI jeeth backend
# ---------------------------------------------------------------------------

# AES-128-CBC key / IV are hard-coded in the SPA's ``main.<hash>.js`` bundle.
# Both decoded as Latin-1 16-byte strings.
_AES_KEY = b"aar6tzij8o1snaar"
_AES_IV = b"0123456789ABCDEF"
_API_BASE = "https://itiamc.com/jeeth/api/v1/catalog"

# Map data-month YYYY-MM → fileName fragment ``April 2026`` to match
# the ``fileName`` field in the documentList payload.
def _filename_month_label(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{_MONTH_NAMES[m - 1]} {y}"


def _aes_encrypt(plaintext: str) -> str:
    """AES-128-CBC encrypt + PKCS#7 pad + base64. Imports cryptography
    lazily so a test environment that mocks ``_resolve_pdf_url`` doesn't
    need the dep at import time.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    padder = PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_IV)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(ct).decode("ascii")


def _aes_decrypt(b64_ct: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    raw = base64.b64decode(b64_ct)
    dec = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_IV)).decryptor()
    padded = dec.update(raw) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def _random_guid() -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(32))


def _post_jeeth(endpoint: str, payload: dict) -> dict:
    """POST an encrypted payload to ``/jeeth/api/v1/catalog/<endpoint>``
    and return the decrypted JSON response body. Raises IngestError on
    network failure or non-zero status.
    """
    body = dict(payload)
    body["guid"] = _random_guid()
    body["timeStamp"] = int(time.time() * 1000)
    plaintext = json.dumps(body, separators=(",", ":"))
    e_data = _aes_encrypt(plaintext)
    wrapped = json.dumps({"eData": e_data}).encode()
    req = urllib.request.Request(
        f"{_API_BASE}/{endpoint}",
        data=wrapped,
        headers={
            "Content-Type": "application/json",
            "Origin": "https://itiamc.com",
            "Referer": "https://itiamc.com/",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        r = urllib.request.urlopen(req, timeout=30)
        raw = r.read()
    except urllib.error.HTTPError as e:
        raise IngestError(
            f"iti: jeeth API HTTP {e.code} for endpoint {endpoint!r}: "
            f"{e.read()[:200].decode('utf-8', 'replace')}"
        ) from e
    except urllib.error.URLError as e:
        raise IngestError(f"iti: jeeth API unreachable for {endpoint!r}: {e}") from e
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise IngestError(f"iti: jeeth API non-JSON response: {raw[:200]!r}") from e
    if isinstance(envelope, dict) and envelope.get("eData"):
        body = json.loads(_aes_decrypt(envelope["eData"]))
    else:
        body = envelope
    if body.get("status") != 0:
        raise IngestError(
            f"iti: jeeth API status={body.get('status')!r} message="
            f"{body.get('message')!r} for endpoint {endpoint!r}"
        )
    return body.get("data") or {}


def _resolve_pdf_url(ym: str) -> str:
    """Look up the canonical factsheet PDF URL for data month ``ym``.

    Queries the ``getDocumentsByType`` endpoint with ``{"type":
    "downloads"}`` and selects the entry under the ``Factsheet`` topic
    whose ``fileName`` field equals ``Factsheet - <Month> <YYYY>``
    (case-insensitive). Raises IngestError if no matching entry exists
    (e.g. the month hasn't been published yet) — consistent with the
    fail-fast invariant: never silently fall back to a stale or
    fabricated URL.
    """
    data = _post_jeeth("getDocumentsByType", {"type": "downloads"})
    target_label = _filename_month_label(ym)  # e.g. "April 2026"
    expected_filename = f"Factsheet - {target_label}".lower()
    for group in data.get("documentList", []) or []:
        if (group.get("topic") or "").strip().lower() != "factsheet":
            continue
        for item in group.get("returnList", []) or []:
            file_name = (item.get("fileName") or "").strip().lower()
            if file_name == expected_filename:
                url = item.get("url")
                if not url:
                    raise IngestError(
                        f"iti: factsheet manifest entry for {target_label!r} "
                        "has empty 'url' field"
                    )
                return url
    raise IngestError(
        f"iti: no factsheet URL published for data month {ym!r} "
        f"(looking for fileName={expected_filename!r}). Check whether the "
        "AMC has uploaded the issue yet."
    )


# ---------------------------------------------------------------------------
# Scheme-name + PTR extraction
# ---------------------------------------------------------------------------

# Scheme name shape: line beginning with ``ITI `` and ending in ``Fund`` or
# ``FOF``. The optional parenthetical (``(formerly known as ...)``) is on a
# separate continuation line in pdfplumber's output and not captured here.
_SCHEME_NAME_RE = re.compile(
    r"^(ITI\b[\w\s&\-’'.]*?(?:Fund|FOF))\s*$",
    re.IGNORECASE,
)

# Printed-name normalization for the shared fuzzy matcher. The factsheet
# titles the Large-and-Midcap scheme "ITI Large & Mid Cap Fund" — "Mid Cap"
# as two words. The matcher uses token_set_ratio, which treats the token set
# {large, cap} of "ITI Large Cap Fund" as a SUBSET of {large, mid, cap} and so
# scores BOTH printed names 100 against the Large-Cap scheme, collapsing the
# Large-&-Midcap PTR onto the Large-Cap scheme_code (the rows then dedupe
# away). scheme_master spells the L&M scheme "Large & Midcap" (one token),
# which disambiguates cleanly, so we fold the two-word factsheet spelling onto
# it. CRITICAL: scope the fold to the FULL "Large & Mid Cap" phrase — the
# standalone scheme "ITI Mid Cap Fund" (code 148733) is spelled with two words
# in BOTH the factsheet and scheme_master, so a blanket "Mid Cap"→"Midcap"
# would wrongly drag it onto the Large-&-Midcap code.
_LARGE_MIDCAP_RE = re.compile(
    r"\bLarge\s*&\s*Mid\s+Cap\b", re.IGNORECASE
)

# PTR: ``Portfolio Turnover Ratio 1.08``. ITI prints a fraction directly
# (not a percent) — pass through with no /100 divide.
_PTR_RE = re.compile(r"Portfolio\s+Turnover\s+Ratio\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
# Sentinel forms for newly-launched schemes that haven't completed 1y.
# ITI uses both literal ``NA`` and a bare hyphen ``-`` placeholder.
_PTR_SENTINEL_RE = re.compile(
    r"Portfolio\s+Turnover\s+Ratio\s+(?:NA|-)\b",
    re.IGNORECASE,
)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name, or None for non-scheme pages.

    Scheme detail pages always carry both the ``CATEGORY OF SCHEME`` and
    ``PORTFOLIO DETAILS`` markers in the body — used as an exclusive
    page gate. The actual scheme title is the first line beginning with
    ``ITI `` and ending in ``Fund``/``FOF``, scanned over the first 60
    non-empty lines (page 20 places the title after a chart banner).
    """
    if not text:
        return None
    if "CATEGORY OF SCHEME" not in text or "PORTFOLIO DETAILS" not in text:
        return None
    if "Ready Reckoner" in text[:400]:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines[:60]:
        if ln.lower().startswith("iti mutual fund"):
            continue
        m = _SCHEME_NAME_RE.match(ln)
        if not m:
            continue
        name = m.group(1).strip()
        # Defensive: collapse runs of whitespace.
        name = re.sub(r"\s+", " ", name)
        # Fold "Large & Mid Cap" onto scheme_master's "Large & Midcap" so the
        # fuzzy matcher disambiguates it from the Large-Cap scheme (leaving the
        # standalone "ITI Mid Cap Fund" untouched).
        name = _LARGE_MIDCAP_RE.sub("Large & Midcap", name)
        return name
    return None


@register_adapter
class ItiAdapter(ManagerAdapter):
    """ITI Mutual Fund factsheet adapter.

    ``amc_slug = 'iti'`` matches ``scheme_master.amc_code`` exactly
    (derived from ``ITI Mutual Fund`` by the master-table slugifier), so
    no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "iti"
    source_label = "ITI Mutual Fund"

    # ----------------------------------------------------------------------
    # URL — live discovery via the jeeth API
    # ----------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the canonical ITI combined factsheet PDF URL for data
        month ``ym='YYYY-MM'``.

        Unlike most AMCs, ITI's CMS prepends an opaque upload-time epoch
        prefix (``1779445747-``) to the filename, so the URL cannot be
        constructed from ``ym`` alone. We discover it at runtime by
        calling the AMC's ``getDocumentsByType`` endpoint (encrypted
        payload — see module docstring) and matching the ``fileName``
        field to ``Factsheet - <Month> <YYYY>``. Raises IngestError when
        the issue hasn't been published yet — consistent with the
        project's fail-fast invariants.
        """
        return _resolve_pdf_url(ym)

    # ----------------------------------------------------------------------
    # PTR
    # ----------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        logging.getLogger("pdfminer").setLevel(logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                # Drop newly-launched-scheme sentinels silently (the
                # footnote says "Portfolio turnover ratio not provided.
                # Since the scheme has not completed one year").
                if _PTR_SENTINEL_RE.search(text):
                    continue
                m = _PTR_RE.search(text)
                if not m:
                    continue
                try:
                    ptr_value = float(m.group(1))
                except ValueError:
                    continue
                # Fail-fast: drop NaN / non-positive.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )

    # ----------------------------------------------------------------------
    # Holdings — deferred. ITI's portfolio tables are two-column with
    # sector banners interleaved and no ISINs. Phase 3.A's parallel
    # ISIN-tagged path covers the overlap metric, so we yield nothing.
    # ----------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
