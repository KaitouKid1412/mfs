"""Edelweiss Mutual Fund — factsheet adapter (Phase 3).

Calibrated against the April 2026 combined factsheet at
``data/raw/factsheets/edelweiss/2026-04.pdf`` (~14.8 MB, 190 pages,
~62 scheme pages plus disclosures / IDCW history / performance tables
in the back matter).

URL discovery
-------------
Edelweiss publishes monthly factsheets via a JS-rendered SPA whose
download list is served from a CryptoJS-AES-encrypted API at
``https://api.edelweissmf.com/edelweissmf/api/v1/third-party/getAllDownloads``.
Per-month PDFs are hosted under
``https://www.edelweissmf.com/Files/downloads/FACTSHEETS/FACTSHEETS/<YYYY>/<Mmm>/<status>/Edelweiss_..._<MMMM>_<YYYY>_<DDMMYYYY>_<HHMMSS>_<AM|PM>.pdf``
where:

- the **publish-month** folder reflects the **data month + 1** (the
  April-2026-data factsheet is uploaded in May 2026, under ``2026/May/Published/``),
- the filename root encodes the publish month name and year, followed
  by an upload-timestamp suffix that is not predictable month-over-month,
- file naming has been inconsistent over the years (Edelweiss_MF_Factsheet,
  Edelweiss_Factsheet, with or without underscores around the month).

Because the timestamp suffix changes every release, ``build_url(ym)``
returns the **CMS listing URL** (the public SPA route the user would
follow) for documentation purposes. The ``fetch()`` override resolves the
real PDF URL by calling the encrypted ``getAllDownloads`` API,
decrypting the response with the per-request HmacSHA256 key, and
selecting the entry whose filename matches the data month.

Layout findings driving the parser
----------------------------------

* Each scheme page's first non-empty line starts with ``Edelweiss `` and
  carries the scheme name. Long names wrap to a second line (e.g.
  ``Edelweiss Large & Mid Cap`` + ``Fund``, ``Edelweiss Multi Asset`` +
  ``Allocation Fund``). We detect wrap by checking whether line 1 ends
  in ``Fund``/``FOF``/``FoF``/``ETF`` — if not, we join line 2 onto it
  up to the first ``Fund/FOF/ETF`` token. Some "secondary" continuation
  pages (the portfolio-detail page that follows each main scheme page)
  also start with the same scheme name, but they carry no PTR/AUM
  block; the parser yields nothing for those, and the orchestrator's
  PK-dedupe keeps just the first row per (scheme, month).

* PTR is printed two different ways:
    - **Active equity** pages: ``Portfolio Turnover Ratio3 : 0.79`` on a
      single line in the left column. Value is the next ``\\d+\\.\\d+``
      token on the same y-band, after the ``Ratio`` word.
    - **Index / passive** pages: the label appears as a column header
      below a quantitative-indicators block (``Total number of equity
      stocks | Top 10 stocks | Portfolio Turnover Ratio``), with the
      value rendered as a separate numeric row ABOVE the header. Value
      is the ``\\d+\\.\\d+`` token in the same column, ~10pt above the
      ``Portfolio`` word.
  PTR is **already a fraction** in both layouts (Edelweiss Large Cap
  Fund prints ``0.77`` = 77% turnover) — no percent→fraction conversion.
  We deliberately use the FIRST ``Portfolio Turnover Ratio`` anchor on
  the page (the second one on equity pages is the bottom-of-page note
  ``2. Portfolio Turnover Ratio: Lower of sales or purchase divided...``,
  not a data value).

* Pages 1-5 (cover / TOC / market commentary), 70+ debt detail / 130+
  IDCW history / performance disclosure pages either don't start with
  ``Edelweiss <X> Fund`` or have no PTR marker, and are silently skipped.

Calibration counts on 2026-04
-----------------------------
* ~62 unique scheme pages detected.
* ~28 PTR rows extracted (active equity + passive index funds; pure
  debt / arbitrage / ETF pages legitimately don't print PTR).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from typing import Iterable

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
_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _publish_ym(data_ym: str) -> tuple[int, int]:
    """Publish month = data month + 1 (with year rollover).

    The April-2026 data factsheet is published in May 2026.
    """
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return y + 1, 1
    return y, m + 1


# -----------------------------------------------------------------------------
# Encrypted CMS API helpers
# -----------------------------------------------------------------------------

# The SPA's HTTP interceptor adds ``x-ip-address`` and ``x-timestamp`` headers,
# then derives a per-request AES-256 passphrase via:
#   key = HmacSHA256(secreat + ip + timestamp, hashKey).hex()
# Response bodies are CryptoJS OpenSSL-format ciphertext (``Salted__`` +
# 8 bytes salt + AES-256-CBC) base64-encoded under ``{"body": "..."}``.
_API_SECREAT = "5b6714126d3149fbab994747b2633287"
_API_HASHKEY = "r4vcos0ejvndsow95n"
# The SPA falls back to this constant IP when ipify.org is unreachable; it
# works server-side too because Edelweiss only HMACs the value into the key,
# never actually validates the source IP.
_API_STATIC_IP = "103.0.123.175"
_API_BASE = "https://api.edelweissmf.com/edelweissmf/api/v1"
_API_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)


def _evp_bytes_to_key(passphrase: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16):
    """Replicate OpenSSL/CryptoJS EVP_BytesToKey (MD5)."""
    dt = b""
    out = b""
    while len(out) < key_len + iv_len:
        dt = hashlib.md5(dt + passphrase + salt).digest()
        out += dt
    return out[:key_len], out[key_len : key_len + iv_len]


def _decrypt_openssl(b64_ct: str, passphrase: str) -> bytes:
    """Decrypt a CryptoJS-OpenSSL-formatted ciphertext (Salted__ + AES-CBC).

    Uses ``cryptography`` (already a project dep), not pycryptodome.
    """
    # Local import keeps the cryptography hazmat path out of the
    # import-time hot path for adapters that don't need the API client.
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = base64.b64decode(b64_ct)
    if raw[:8] != b"Salted__":
        raise IngestError(
            f"edelweiss: unexpected CryptoJS ciphertext header {raw[:16]!r}"
        )
    salt, ct = raw[8:16], raw[16:]
    key, iv = _evp_bytes_to_key(passphrase.encode(), salt)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(ct) + decryptor.finalize()
    pad = plain[-1]
    if not (1 <= pad <= 16):
        raise IngestError("edelweiss: bad PKCS#7 padding after AES decrypt")
    return plain[:-pad]


def _api_get_decrypted(client: httpx.Client, path: str) -> dict:
    """GET an Edelweiss API endpoint and return its decrypted JSON body."""
    ts = str(int(time.time() * 1000))
    ip = _API_STATIC_IP
    yt = hmac.new(
        _API_HASHKEY.encode(),
        (_API_SECREAT + ip + ts).encode(),
        hashlib.sha256,
    ).hexdigest()
    url = f"{_API_BASE}/{path.lstrip('/')}"
    try:
        r = client.get(url, headers={"x-ip-address": ip, "x-timestamp": ts})
    except httpx.HTTPError as e:
        raise IngestError(f"edelweiss: API request failed for {url}: {e}") from e
    if r.status_code != 200:
        raise IngestError(
            f"edelweiss: API {url} returned HTTP {r.status_code}: {r.text[:200]}"
        )
    try:
        body = r.json()["body"]
    except (ValueError, KeyError) as e:
        raise IngestError(f"edelweiss: API {url} returned non-JSON or missing body") from e
    plain = _decrypt_openssl(body, yt)
    return json.loads(plain.decode("utf-8", errors="replace"))


def _new_browser_client() -> httpx.Client:
    """Return an httpx client tuned to look like a real browser.

    Edelweiss sits behind Akamai which rejects requests with the default
    httpx/curl TLS fingerprint. HTTP/2 + a realistic User-Agent + Sec-Fetch-*
    headers plus a homepage warmup get us past the bot filter.
    """
    return httpx.Client(
        http2=True,
        headers={
            "User-Agent": _API_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.edelweissmf.com",
            "Referer": "https://www.edelweissmf.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        },
        timeout=60,
        follow_redirects=True,
    )


def _walk_pdf_paths(node, out: list[str]) -> None:
    """Recursively collect all PDF-like string values from a nested JSON node."""
    if isinstance(node, dict):
        for v in node.values():
            _walk_pdf_paths(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_pdf_paths(v, out)
    elif isinstance(node, str):
        if ".pdf" in node.lower():
            out.append(node)


# -----------------------------------------------------------------------------
# PDF parsers
# -----------------------------------------------------------------------------

# Single-line PTR (active equity pages): "Portfolio Turnover Ratio3 : 0.79".
_PTR_INLINE_RE = re.compile(
    r"Portfolio\s+Turnover\s+Ratio[\d,\s\^]*[:\-]?\s*(\d+\.\d+)",
    re.IGNORECASE,
)

# Suffixes that mark a complete scheme title (case-insensitive).
_NAME_TERMINATORS = re.compile(r"\b(Fund(?:\s+of\s+Funds?)?|FOF|FoF|ETF)\b", re.IGNORECASE)
# Trailing footnote / asterisk-tag glyphs that AMCs sometimes attach.
_NAME_TRAILING_GLYPHS_RE = re.compile(r"[\s*\^@\$#]+$")


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from a scheme page, or None.

    Scheme pages start with a first non-empty line ``Edelweiss <name...>``.
    Logic:

    * If line 1 already ends with ``Fund``/``FOF``/``FoF``/``ETF`` (after
      stripping trailing footnote glyphs), use the substring up to that
      terminator. This is the common case for single-line names.
    * Otherwise, join line 1 + line 2 and pick the LAST
      ``Fund``/``FOF``/``FoF``/``ETF`` token in the joined string. This
      handles wrapped names like ``Edelweiss Silver ETF Fund of\\nFund``
      (where ``ETF`` is a mid-name token, not the terminator) and
      ``Edelweiss Multi Asset\\nAllocation Fund``.

    Cover / TOC / commentary / disclosure pages start with something
    else and are rejected.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.startswith("Edelweiss"):
        return None
    # Strip footnote markers from the end of line 1 before terminator check.
    first_clean = _NAME_TRAILING_GLYPHS_RE.sub("", first)
    # Common case: line 1 ends with a Fund/ETF/FOF token.
    end_match = re.search(
        r"\b(Fund(?:\s+of\s+Funds?)?|FOF|FoF|ETF)$",
        first_clean,
        re.IGNORECASE,
    )
    if end_match:
        return first_clean
    # Wrap case: extend onto line 2 and pick the last terminator.
    if len(lines) >= 2:
        combined = f"{first_clean} {lines[1]}"
        matches = list(_NAME_TERMINATORS.finditer(combined))
        if matches:
            last = matches[-1]
            return _NAME_TRAILING_GLYPHS_RE.sub(
                "", combined[: last.end()].strip()
            )
    return None


def _find_ptr_via_words(page) -> float | None:
    """Locate the Portfolio Turnover Ratio value via word positions.

    Two layouts are observed:
    1. **Active equity** (page 6 etc.): ``Portfolio Turnover Ratio : 0.79``
       on a single y-band in the left column — the value is the next
       ``\\d+\\.\\d+`` token to the right on the same y.
    2. **Index / passive** (page 89 etc.): the label is a 2-line
       column header — ``Portfolio`` on line 1, ``Turnover Ratio`` on
       line 2 — with the value rendered as a numeric row directly ABOVE
       the ``Portfolio`` word in the same column.

    We deliberately bind to the FIRST ``Portfolio + Turnover + Ratio``
    triple from the top of the page so we don't accidentally match the
    bottom-of-page footnote ``2. Portfolio Turnover Ratio: Lower of...``
    which is explanatory text, not a data value.
    """
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None

    # Find anchor: 'Portfolio' word that has 'Turnover' immediately to its
    # right (same y, x-adjacent) AND a 'Ratio' word adjacent to 'Turnover'
    # (either same y or one line below).
    anchors: list[tuple[dict, dict, dict]] = []
    for i, w in enumerate(words):
        if w["text"] != "Portfolio":
            continue
        for j in range(i + 1, min(i + 4, len(words))):
            v = words[j]
            if v["text"] != "Turnover":
                continue
            # Turnover can be on same y (active layout) or directly below
            # (index layout 2-line header).
            same_y = abs(v["bottom"] - w["bottom"]) < 3
            below = (w["bottom"] < v["bottom"] < w["bottom"] + 12) and abs(v["x0"] - w["x0"]) < 6
            if not (same_y or below):
                continue
            for k in range(j + 1, min(j + 4, len(words))):
                r = words[k]
                if not r["text"].startswith("Ratio"):
                    continue
                # Active: Ratio same y as Turnover, x-adjacent.
                # Index: Ratio same y as Turnover, x-adjacent.
                if abs(r["bottom"] - v["bottom"]) < 3 and 0 < r["x0"] - v["x1"] < 20:
                    anchors.append((w, v, r))
                    break
            break
    if not anchors:
        return None
    anchors.sort(key=lambda t: t[0]["top"])  # top-most first
    portfolio, turnover, ratio = anchors[0]

    # Strategy 1: same-y value to the right of 'Ratio' (active layout).
    anchor_y = portfolio["bottom"]
    for v in words:
        if abs(v["bottom"] - anchor_y) > 3:
            continue
        if v["x0"] <= ratio["x1"]:
            continue
        if v["x0"] > ratio["x1"] + 250:
            continue
        if re.fullmatch(r"\d+\.\d+", v["text"]):
            try:
                return float(v["text"])
            except ValueError:
                continue

    # Strategy 2: value ABOVE the header in the same column (index layout).
    # Use the column centred on the ``Portfolio`` x-position (the header
    # word that sits at the start of the column).
    col_x = portfolio["x0"]
    cands = []
    for v in words:
        if v["top"] >= portfolio["top"]:
            continue
        if v["top"] < portfolio["top"] - 40:
            continue
        # Index-layout values are roughly centred under/over the column
        # header at ``portfolio["x0"]+~40``. We allow a ±50pt window so
        # tokens of varying width (1-3 chars to left, decimals to right)
        # are all captured.
        if abs((v["x0"] + v["x1"]) / 2 - (portfolio["x0"] + ratio["x1"]) / 2) > 50:
            continue
        if re.fullmatch(r"\d+\.\d+", v["text"]):
            cands.append(v)
    if cands:
        cands.sort(key=lambda c: c["top"], reverse=True)  # closest above
        try:
            return float(cands[0]["text"])
        except ValueError:
            return None
    return None


@register_adapter
class EdelweissAdapter(ManagerAdapter):
    """Edelweiss Mutual Fund factsheet adapter.

    ``amc_slug = "edelweiss"`` matches ``scheme_master.amc_code`` directly,
    so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "edelweiss"
    source_label = "Edelweiss Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical CMS listing URL for the data month.

        Edelweiss generates a unique upload-timestamp suffix in every
        PDF filename, so a deterministic direct-PDF URL cannot be
        constructed from ``ym`` alone. The listing URL below is the
        public route a user would follow to find the file; the actual
        PDF URL is resolved at fetch time by calling the
        ``getAllDownloads`` API (see :meth:`fetch`).
        """
        return "https://www.edelweissmf.com/downloads/factsheets"

    def fetch(self, ym: str) -> Path:
        """Download the Edelweiss monthly factsheet for data month ym.

        Resolves the per-month PDF URL by querying the encrypted
        ``third-party/getAllDownloads`` API, decrypting the response,
        and matching the entry whose filename embeds the publish month
        (data month + 1) and year.

        Re-uses an existing cached PDF unconditionally — delete the file
        to force a re-fetch.
        """
        from mfs import paths
        from mfs.io.http import download_to

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out

        publish_year, publish_month_idx = _publish_ym(ym)
        publish_month_name = _MONTH_NAMES[publish_month_idx - 1]
        # The CMS organises files under <YYYY>/<Mmm>/, but the YYYY field
        # has been miscatalogued in the past (legacy 2020/Apr folder for
        # an April 2026 file). We match against the **filename** content
        # rather than the folder path so the parser is resilient.

        client = _new_browser_client()
        try:
            # Warm the connection — Akamai requires the homepage hit to
            # establish a TLS session before letting API calls through.
            client.get("https://www.edelweissmf.com/")
            data = _api_get_decrypted(client, "third-party/getAllDownloads")
        except IngestError:
            client.close()
            raise

        candidates: list[str] = []
        _walk_pdf_paths(data, candidates)
        # Combined factsheet PDFs live under ``/Files/downloads/FACTSHEETS/``.
        # Scheme-specific factsheets (e.g. NIFTY PSU Bond Plus SDL Apr-2026
        # Fund) live under ``/Files/downloads/Product Collateral/Factsheet/``
        # and must NOT be selected.
        publish_short = _MONTH_ABBR[publish_month_idx - 1]
        wanted: list[str] = []
        for path in candidates:
            if "/Files/downloads/FACTSHEETS/" not in path:
                continue
            lower = path.lower()
            # The filename must reference the publish month name and year.
            # Edelweiss uses underscores or dashes around the month token
            # (``Factsheet_May_-_2026``, ``Factsheet_April_2026``,
            # ``Factsheet_February-2026``).
            if str(publish_year) not in lower:
                continue
            month_in_name = (
                f"_{publish_month_name.lower()}_" in lower
                or f"-{publish_month_name.lower()}-" in lower
                or f"_{publish_month_name.lower()}-" in lower
                or f"-{publish_month_name.lower()}_" in lower
                or f"_{publish_short.lower()}_" in lower
            )
            if not month_in_name:
                continue
            wanted.append(path)

        if not wanted:
            client.close()
            raise IngestError(
                f"edelweiss: no combined factsheet PDF found for publish "
                f"month {publish_month_name} {publish_year} (data month "
                f"{ym}) under /Files/downloads/FACTSHEETS/ — CMS layout "
                "may have changed."
            )
        client.close()
        # If multiple, pick the latest one by upload timestamp (last 17
        # chars before ``.pdf`` are ``DDMMYYYY_HHMMSS_AM|PM``). Falling
        # back to lexical max is safe since the timestamp suffix sorts
        # monotonically within a publish-month.
        url = max(wanted)
        if url.startswith("/"):
            url = "https://www.edelweissmf.com" + url
        # Use the project's standard downloader so retries / archiving are
        # consistent with the other adapters.
        log.info("edelweiss.resolved_pdf_url", ym=ym, url=url)
        return download_to(url, out)

    # ------------------------------------------------------------------
    # PTR
    # ------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        seen: set[str] = set()
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                # Prefer the inline text-extract regex (cheap, works on
                # active-equity pages). Fall back to word positions for
                # index/passive pages where the value is column-stacked
                # above the label.
                ptr_value: float | None = None
                m = _PTR_INLINE_RE.search(text)
                if m:
                    try:
                        ptr_value = float(m.group(1))
                    except ValueError:
                        ptr_value = None
                if ptr_value is None:
                    ptr_value = _find_ptr_via_words(page)
                if ptr_value is None:
                    continue
                # Fail-fast: drop NaN / non-positive. Edelweiss prints a
                # real fraction or omits the field; zero PTR is implausible
                # for an active scheme.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                # Dedupe by scheme name on this page so we don't double-
                # emit when the continuation page also matches a header.
                if scheme in seen:
                    continue
                seen.add(scheme)
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )

    # ------------------------------------------------------------------
    # Holdings — deferred. Edelweiss's portfolio table is a two-column
    # equity layout (Company Name / Allocation) with no ISINs printed,
    # mirroring HDFC / Kotak. Phase 3.A will calibrate the x-bands; for
    # now we rely on the ISIN-tagged Excel ingest path.
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
