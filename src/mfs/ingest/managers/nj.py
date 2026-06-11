"""NJ Mutual Fund — factsheet adapter (Phase 3 PTR coverage).

Calibrated against the combined monthly factsheet reporting data **as on
30 April 2026** (``data/raw/factsheets/nj/2026-04.pdf``, ~0.47 MB, 14 pages).
NJ names the file by **data month** (``...Factsheet-April-2026...``) even though
the April-end issue is *published* in May (the cached file's timestamp suffix
is ``20260508``). So ``build_url('2026-04')`` resolves the ``April-2026`` href —
NO publish-month shift like HDFC/Bajaj.

URL resolution (the hard part)
------------------------------
There is **no deterministic PDF URL**. NJ serves every download through
``https://downloads.njmutualfund.com/viewfile.php?file=<NAME>.pdf`` where
``<NAME>`` carries (a) a non-deterministic upload-timestamp suffix
(``-20260508052134``) and (b) a prefix that has churned month-to-month::

    NJ-AMC-Factsheet-April-2026-20260508052134.pdf        (Apr-2026 data)
    136720-NJ-AMC-Factsheet-March-2026-20260407095703.pdf (Mar-2026 data)
    NJ-Mutual-Factsheet-February-2026-20260312060803.pdf  (Feb-2026 data)
    NJ-MF---Factsheet---January-2026-20260210053915.pdf   (Jan-2026 data)
    NJ-Mutual-Fund-Factsheet-Feb-2025-20250310013938.pdf  (abbrev month)

The one stable signal is that the filename always contains ``Factsheet`` plus
the data month (full OR 3-letter abbreviated) and the data year. We therefore:

1. GET the public downloads listing page (``downloads.php``).
2. Scan every ``viewfile.php?file=...Factsheet...`` href and keep the one
   whose filename contains ``Factsheet`` + the data month (full or abbrev) +
   ``-<year>`` (anchored so the year doesn't match inside the timestamp).
3. If several match (rare reissues), pick the one with the largest trailing
   upload timestamp — the most recently uploaded.

Any failure (page down, month not yet published, layout change) raises
``IngestError`` — we never fabricate a URL (the timestamp suffix is
non-deterministic).

Layout findings driving the PTR parser
--------------------------------------
* Each scheme prints across two pages. The **first** page of each scheme is a
  banner page whose lines are ``#BuiltOnRules`` / ``NJ <SCHEME NAME>`` /
  ``<one-line scheme objective>`` / ``Report as on April 30, 2026``. The PTR /
  AUM / TER stats block lives only on this first page; the second page is the
  ``PORTFOLIO CLASSIFICATION ...`` continuation. We anchor on the
  ``#BuiltOnRules`` header so the SIP-returns, glossary, and disclosure pages
  (which also mention scheme names) are excluded.

* PTR is printed as ``Portfolio Turnover Ratio 0.84`` — a **fraction** (a
  ratio, not a percent). Storage is also a fraction, so we pass through with
  NO /100 conversion. Sanity check on the cached PDF: Flexi Cap 0.84, ELSS
  0.56, Balanced Advantage 3.51, Arbitrage 9.34 — all sane fractions (an
  arbitrage fund legitimately churns ~9x/yr; a percent reading would imply a
  nonsensical 0.84%-of-portfolio annual turnover for an equity fund).

* On some pages the ``Portfolio Turnover Ratio`` line carries right-column
  holdings-table bleed in ``extract_text`` (e.g. ``Portfolio Turnover Ratio
  9.34 Godrej Properties Limited Realty 0.10%``) and on others a left-column
  ``Modified Duration* 30 Days`` prefix. The value always immediately follows
  the label, so a single anchored text regex handles every case — no
  word-position fallback is needed.

* The glossary page ("HOW TO READ A MUTUAL FUND FACTSHEET?") prints the bare
  label ``Portfolio Turnover Ratio:`` with no number; the digit-requiring
  regex skips it. The Overnight (debt) fund omits the metric entirely — it
  yields nothing (fail-fast: no half-data).

Calibration counts on 2026-04
-----------------------------
* 5 scheme banner pages detected (Flexi Cap, ELSS Tax Saver, Balanced
  Advantage, Arbitrage, Overnight).
* 4 PTR records parsed (Flexi Cap 0.84, ELSS 0.56, Balanced Advantage 3.51,
  Arbitrage 9.34). The Overnight debt fund legitimately omits PTR and is
  dropped — covering all 3 ranked equity funds (Flexi Cap, ELSS, BAF) plus
  Arbitrage.

``amc_slug = 'nj'`` matches ``scheme_master.amc_code`` directly, so no alias
entry in ``_scheme_match.py`` is required.

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

_MONTH_FULL = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# A browser-style UA — defensive against any WAF UA filtering on the host
# (the AMFI default UA is accepted but we stay conservative).
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_DOWNLOADS_HOST = "https://downloads.njmutualfund.com"
_DOWNLOADS_PAGE = f"{_DOWNLOADS_HOST}/downloads.php"

# Any factsheet download href on the listing page.
_FACTSHEET_HREF_RE = re.compile(
    r'href="((?:viewfile\.php\?file=|[^"]*?/)?[^"]*?[Ff]actsheet[^"]*?\.pdf)"',
    re.IGNORECASE,
)
# Trailing upload-timestamp (14 digits) used to pick the freshest reissue.
_UPLOAD_TS_RE = re.compile(r"(\d{14})\.pdf$", re.IGNORECASE)


def _month_token_re(month_idx: int) -> re.Pattern[str]:
    """Regex matching the data month (full OR 3/4-letter abbrev) in a filename.

    NJ filenames use either ``-April-2026-`` or abbreviated ``-Feb-2025-`` /
    ``-Oct-2025-`` forms. We accept both. The month token must be bounded by a
    non-letter on each side so ``Mar`` does not match inside ``March``'s
    siblings, etc.
    """
    forms = {_MONTH_FULL[month_idx]}
    # Map full month to its abbreviation(s).
    abbr_map = {
        0: ("Jan",), 1: ("Feb",), 2: ("Mar",), 3: ("Apr",), 4: ("May",),
        5: ("Jun",), 6: ("Jul",), 7: ("Aug",), 8: ("Sep", "Sept"),
        9: ("Oct",), 10: ("Nov",), 11: ("Dec",),
    }
    forms.update(abbr_map[month_idx])
    alt = "|".join(sorted(forms, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z])(?:{alt})(?![A-Za-z])", re.IGNORECASE)


def _resolve_factsheet_url(ym: str) -> str:
    """Resolve the combined factsheet PDF URL for data month ym='YYYY-MM'.

    NJ names the file by data month (no publish shift), so we look for a
    ``...Factsheet...<MonthName>-<Year>...`` href on the public downloads page.
    Returns the absolute URL. Raises ``IngestError`` on any failure — we never
    fabricate the non-deterministic timestamp suffix.
    """
    year, month = map(int, ym.split("-"))
    month_re = _month_token_re(month - 1)
    # Year must be anchored so it doesn't match inside the 14-digit timestamp.
    year_re = re.compile(rf"(?<!\d){year}(?!\d)")

    try:
        with httpx.Client(
            timeout=90.0,
            headers={"User-Agent": _BROWSER_UA, "Accept": "*/*"},
            follow_redirects=True,
        ) as c:
            page = c.get(_DOWNLOADS_PAGE)
            page.raise_for_status()
            html = page.text
    except Exception as e:  # noqa: BLE001
        raise IngestError(
            f"nj: failed to load downloads page {_DOWNLOADS_PAGE}: {e}"
        ) from e

    candidates: list[str] = []
    for href in _FACTSHEET_HREF_RE.findall(html):
        fname = href.split("file=", 1)[-1]
        # Require BOTH the data month and the data year in the filename, each
        # anchored against the timestamp digits.
        if not month_re.search(fname):
            continue
        if not year_re.search(fname):
            continue
        candidates.append(href)

    if not candidates:
        raise IngestError(
            f"nj: no factsheet href for {_MONTH_FULL[month - 1]} {year} on "
            f"{_DOWNLOADS_PAGE} (issue may not be published yet, or the "
            "filename convention changed)."
        )

    # Prefer the freshest reissue (largest trailing upload timestamp).
    def _ts_key(href: str) -> str:
        m = _UPLOAD_TS_RE.search(href)
        return m.group(1) if m else ""

    href = max(candidates, key=_ts_key)
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return f"{_DOWNLOADS_HOST}/{href.lstrip('/')}"


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------

# NJ scheme banner pages lead with ``#BuiltOnRules`` then ``NJ <NAME>``. We
# anchor on the header so SIP-returns / glossary / disclosure pages (which
# mention scheme names elsewhere) are excluded.
_HEADER_ANCHOR = "#BuiltOnRules"
_BANNER_RE = re.compile(r"^NJ [A-Z]")


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name for a scheme banner page, or None.

    On every per-scheme banner page the first non-empty line is the
    ``#BuiltOnRules`` header and the next non-empty line is the ``NJ <SCHEME
    NAME>`` banner (e.g. ``NJ FLEXI CAP FUND``). We require the header anchor
    so non-scheme pages are dropped, and take the banner as the scheme name.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0] != _HEADER_ANCHOR:
        return None
    for ln in lines[1:4]:
        if _BANNER_RE.match(ln):
            return re.sub(r"\s+", " ", ln).strip() or None
    return None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# NJ prints PTR as a FRACTION (a ratio): ``Portfolio Turnover Ratio 0.84``.
# Storage is also a fraction, so we pass through (NO /100 conversion). The
# value always immediately follows the label even when left-column duration
# text prefixes it or right-column holdings text trails it.
_PTR_RE = re.compile(
    r"Portfolio\s+Turnover\s+Ratio\s+(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


@register_adapter
class NjAdapter(ManagerAdapter):
    """NJ Mutual Fund factsheet adapter."""

    amc_slug = "nj"
    source_label = "NJ Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the combined-factsheet PDF URL for data month ym='YYYY-MM'.

        NJ's ``viewfile.php`` filenames carry a non-deterministic upload
        timestamp and a churning prefix, so the URL is resolved live from the
        public downloads listing. Raises ``IngestError`` if the issue isn't
        published yet or the page changes shape.
        """
        return _resolve_factsheet_url(ym)

    def fetch(self, ym: str) -> Path:
        """Resolve and download the data-month factsheet to the cache path.

        Overrides the base ``fetch`` because (a) ``build_url`` performs a
        network resolution we don't want to repeat on cache hits, and (b) we
        carry a browser UA through to the download host. Re-uses an existing
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
            raise IngestError(f"nj: failed to download {url}: {e}") from e
        if not payload.startswith(b"%PDF"):
            raise IngestError(
                f"nj: downloaded content from {url} is not a PDF "
                f"(first bytes: {payload[:16]!r})."
            )
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
                    # Debt / overnight pages omit the block entirely — drop
                    # (fail-fast: no half-data).
                    continue
                try:
                    ptr = float(m.group(1))
                except ValueError:
                    continue
                # Already a fraction — pass through. Drop NaN / non-positive.
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
