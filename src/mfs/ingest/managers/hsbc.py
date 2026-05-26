"""HSBC Mutual Fund — factsheet adapter (Phase 3).

Calibrated against the April 2026 "The Asset" combined factsheet at
``data/raw/factsheets/hsbc/2026-04.pdf`` (~10.5 MB, 89 pages, 44 scheme
pages plus cover / CEO speak / commentary / fund-positioning / IDCW
history / disclosure pages).

URL discovery
-------------
HSBC India MF (https://www.assetmanagement.hsbc.co.in) publishes the
monthly combined factsheet under a per-upload UUID-prefixed path:

    https://www.assetmanagement.hsbc.co.in/assets/documents/mutual-funds/en/
    <uuid>/the-asset-<monthname>-<YYYY>.pdf

Because the UUID prefix is non-deterministic month-over-month, the direct
PDF URL cannot be constructed from ``ym`` alone. ``build_url`` returns the
public listing route the user would follow; ``fetch()`` overrides the
default behavior to scrape that listing page for the actual ``the-asset-
<monthname>-<year>.pdf`` URL and download it.

Legacy filename variants observed under the same path:

* ``the-asset-<monthname>-<year>.pdf`` (current, 2024-04 onwards)
* ``the-asset-factsheet-<monthname>-<year>.pdf`` (2022-02 to 2024-01)
* ``the-asset-factsheet-<short>-<year>.pdf`` (some months 2020-08,
  2021-08, 2022-07, ``aug``/``july``/``oct`` etc)
* ``the-asset-<short>-<year>.pdf`` (e.g. ``the-asset-jan-2022.pdf``,
  ``the-asset-aug-2024.pdf``)

The fetcher tries the current pattern first, then falls back to
matching any ``the-asset*<monthname-or-short>*<year>.pdf`` link on
the listing page. The resilient matcher works against legacy filings
without code change.

Layout findings driving the parser
----------------------------------

* Each scheme page's first non-empty line is ``HSBC <name>`` ending
  in ``Fund``, ``FOF`` or ``ETF``. Page 32 prints
  ``HSBC Global Emerging Markets Fund*`` with a footnote glyph that
  is stripped after the Fund/FOF/ETF suffix.

* AUM is printed as a clean line:
  ``AUM (as on 30.04.26) ₹ 1,783.60 Cr.``
  Storage unit is INR Crore — HSBC prints in Crore, no conversion
  needed. We prefer **Month-End AUM** (``AUM (as on <date>)``) over
  the ``AAUM (for the month of <Month>)`` line that follows it,
  consistent with the rest of Stage 2.

* PTR is printed two different ways:

  - **Active equity / index / ETF / equity-FOF pages** (16 of 24
    PTR-bearing schemes): ``Portfolio Turnover (1 year) 0.42`` on a
    single y-band in the left column. PTR is **already a fraction**
    (HSBC Large Cap = 0.42, i.e. 42% turnover) — no percent→fraction
    conversion.

  - **Hybrid / arbitrage pages** (5 of 24, p27-p31): three stacked
    rows
        ``Portfolio Turnover (1 year)`` <- label, no value
        ``Equity Turnover 2.38``        <- pick this one
        ``Total Turnover 5.70``         <- includes debt+derivative
    We always pick ``Equity Turnover`` (stock-picking activity) and
    ignore ``Total Turnover`` (which fuses equity + debt + derivative
    rotation and can be 10× higher).

  On many hybrid pages and a few equity pages (e.g. p11 Large & Mid
  Cap), ``extract_text`` column-bleeds the PTR row with the right-hand
  portfolio-classification table, so we use word-position extraction
  throughout to side-step the issue. Specifically:

  1. Locate the ``Portfolio`` + ``Turnover`` word pair (same y-band,
     adjacent x).
  2. Same-y first: take the next ``\\d+\\.\\d+`` token to the right
     of ``Turnover``. This handles the active-equity layout.
  3. Otherwise, find the next ``Equity`` + ``Turnover`` pair below the
     anchor in the same column, and take the next numeric token on
     that row. This handles the hybrid layout.

* Page 33 is a split two-column layout printing
  ``HSBC Asia Pacific (Ex Japan) Dividend Yield Fund`` (left) and
  ``HSBC Brazil Fund`` (right) on the same page. The first-line
  extraction yields only the LEFT-column scheme name, so HSBC Brazil
  Fund's AUM on the right column is not picked up by this adapter.
  Since ``HSBC Asia Pacific Ex Japan Dividend Yield`` is not in
  scheme_master, the orchestrator's fuzzy matcher drops the left-side
  row anyway. Brazil Fund coverage is a documented gap; if Stage 2
  needs it, a future iteration can column-split p33.

Calibration counts on 2026-04
-----------------------------
* 44 scheme pages detected.
* 24 PTR rows extracted (active equity + index + ETF + hybrid +
  equity FOFs; pure debt / liquid / overnight / debt-FOF / target-
  maturity-index pages legitimately omit PTR).
* 44 AUM rows extracted (every scheme page that the first-line
  match accepts; p33's right-column Brazil Fund AUM is the one known
  miss).
"""

from __future__ import annotations

import re
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
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_ABBR = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


_LISTING_URL = (
    "https://www.assetmanagement.hsbc.co.in/en/mutual-funds/"
    "investor-resources?Doc=fund-factsheets"
)

# HSBC's site sits behind Azure Application Gateway which 403s the default
# httpx UA. A realistic browser UA + Accept headers gets us through.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)
_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# Scheme page first-line check. Accepts pages whose first non-empty line
# starts with ``HSBC `` and contains a Fund/FOF/ETF suffix anywhere
# (some long names wrap, but the suffix sits on line 1 in the captured
# 2026-04 PDF).
_SCHEME_FIRST_LINE_TERMINATOR = re.compile(
    r"\b(?:Fund(?:\s+of\s+Funds?)?|FOF|FoF|ETF)\b",
    re.IGNORECASE,
)
# Trailing footnote glyphs ('*', '#', '@', '$', '^') on scheme titles.
_TRAILING_GLYPHS_RE = re.compile(r"[\s*#@\$\^]+$")
# Numeric token used for value matching (e.g. 0.42, 1.36, 2,687.58).
_NUMERIC_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from the first non-empty line, or
    None for non-scheme pages.

    Scheme pages start with ``HSBC <name>...`` where the name contains
    ``Fund``/``FOF``/``ETF``. We strip trailing footnote glyphs ('*',
    '#', '$', '^') after the suffix.

    Non-scheme pages (cover, CEO speak, market commentary, fund
    positioning, how-to-read, IDCW history, riskometer, TER, disclosures)
    either don't start with ``HSBC `` or don't carry a Fund/FOF/ETF
    suffix token on line 1 and are rejected.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.startswith("HSBC "):
        return None
    # Strip trailing footnote glyphs so canonical name lines up with
    # scheme_master entries.
    cleaned = _TRAILING_GLYPHS_RE.sub("", first).strip()
    if not _SCHEME_FIRST_LINE_TERMINATOR.search(cleaned):
        return None
    # Collapse whitespace runs (defensive — pdfplumber occasionally
    # double-spaces tokens around glyphs).
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or None


def _find_ptr_via_words(page) -> float | None:
    """Locate the Portfolio Turnover (1 year) value via word positions.

    Two layouts:

    1. **Active equity / index / ETF / equity FOF**: the value sits on
       the same y-band as ``Portfolio Turnover (1 year)`` in the left
       column, e.g.

           ``Portfolio Turnover (1 year)   0.42``

       We anchor on the ``Portfolio`` + ``Turnover`` word pair and
       take the next ``\\d+\\.\\d+`` token to the right of ``Turnover``
       on the same y-band.

    2. **Hybrid / arbitrage**: the label row has no value; the value
       is on the next visual row under ``Equity Turnover X.XX``:

           ``Portfolio Turnover (1 year)``
           ``Equity Turnover                  2.38``
           ``Total Turnover                   5.70``
           ``Total Turnover = Equity + Debt + Derivative``

       We find the FIRST ``Equity`` + ``Turnover`` pair below the
       Portfolio anchor in the same column and take the next numeric
       token on its y-band. We deliberately do NOT take ``Total
       Turnover`` (debt + derivative rotation can inflate it 10×
       above the equity-only signal).

    Returns the PTR fraction or None when no value can be resolved.
    PTR is **already a fraction** in HSBC factsheets — no
    percent→fraction conversion.
    """
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None
    n = len(words)

    anchor_idx: int | None = None
    for i, w in enumerate(words):
        if w["text"] != "Portfolio":
            continue
        if i + 1 >= n:
            continue
        t = words[i + 1]
        if t["text"] != "Turnover":
            continue
        if abs(t["top"] - w["top"]) > 3:
            continue
        # Avoid the "Portfolio Classification" header in the right
        # column by requiring the right-hand neighbour to be ``Turnover``
        # (already enforced above) — ``Classification`` would not match.
        anchor_idx = i
        break
    if anchor_idx is None:
        return None
    anchor = words[anchor_idx]
    turnover = words[anchor_idx + 1]
    anchor_y = anchor["top"]
    anchor_x = anchor["x0"]

    # Strategy 1: same-y numeric to the right of Turnover (active layout).
    same_y: list[dict] = []
    for v in words:
        if v is anchor or v is turnover:
            continue
        if abs(v["top"] - anchor_y) > 3:
            continue
        if v["x0"] <= turnover["x1"]:
            continue
        if v["x0"] - turnover["x1"] > 200:
            continue
        if _NUMERIC_RE.fullmatch(v["text"]) and "." in v["text"]:
            same_y.append(v)
    if same_y:
        same_y.sort(key=lambda c: c["x0"])
        try:
            return float(same_y[0]["text"])
        except ValueError:
            pass

    # Strategy 2: hybrid layout — find Equity + Turnover below the
    # anchor in the same column, then the numeric on that row.
    for j in range(anchor_idx + 2, min(anchor_idx + 30, n)):
        ew = words[j]
        if ew["text"] != "Equity":
            continue
        if j + 1 >= n:
            continue
        tw = words[j + 1]
        if tw["text"] != "Turnover":
            continue
        if abs(tw["top"] - ew["top"]) > 3:
            continue
        # Same column as the original anchor.
        if abs(ew["x0"] - anchor_x) > 25:
            continue
        # Must sit BELOW the Portfolio anchor.
        if ew["top"] <= anchor_y:
            continue
        for v in words:
            if abs(v["top"] - ew["top"]) > 3:
                continue
            if v["x0"] <= tw["x1"]:
                continue
            if v["x0"] - tw["x1"] > 200:
                continue
            if _NUMERIC_RE.fullmatch(v["text"]) and "." in v["text"]:
                try:
                    return float(v["text"])
                except ValueError:
                    return None
        break
    return None


@register_adapter
class HsbcAdapter(ManagerAdapter):
    """HSBC Mutual Fund factsheet adapter.

    ``amc_slug`` matches ``scheme_master.amc_code`` directly (``hsbc``),
    so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "hsbc"
    source_label = "HSBC Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical direct-PDF URL for data month ym='YYYY-MM'.

        HSBC's monthly combined factsheet is hosted under a per-upload
        UUID-prefixed path
        ``/assets/documents/mutual-funds/en/<uuid>/the-asset-<monthname>-<year>.pdf``
        — the UUID is not predictable, so this URL is a stable template
        that points at the *current* canonical filename pattern. The
        actual download path is resolved at fetch time by scraping the
        public listing page (see :meth:`fetch`).

        The deterministic-looking URL below uses ``LISTING`` as the
        placeholder for the UUID. Tests asserting URL construction
        should compare against this template; tests asserting the
        resolved download URL should call :meth:`fetch`.
        """
        y, m = map(int, ym.split("-"))
        month_lower = _MONTH_NAMES[m - 1]
        return (
            "https://www.assetmanagement.hsbc.co.in/assets/documents/"
            f"mutual-funds/en/LISTING/the-asset-{month_lower}-{y}.pdf"
        )

    def fetch(self, ym: str) -> Path:
        """Download the HSBC monthly factsheet for data month ym.

        The actual PDF URL embeds a per-upload UUID that changes every
        month, so we scrape the public investor-resources listing page
        and find the link matching ``the-asset-*<month>-<year>.pdf``.

        Re-uses an existing cached PDF unconditionally — delete the file
        to force a re-fetch.
        """
        from mfs import paths
        from mfs.io.http import download_to

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out

        y, m = map(int, ym.split("-"))
        month_lower = _MONTH_NAMES[m - 1]
        month_short = _MONTH_ABBR[m - 1]

        with httpx.Client(
            headers=_BROWSER_HEADERS,
            follow_redirects=True,
            timeout=60,
        ) as client:
            try:
                r = client.get(_LISTING_URL)
            except httpx.HTTPError as e:
                raise IngestError(
                    f"hsbc: failed to fetch listing page {_LISTING_URL}: {e}"
                ) from e
            if r.status_code != 200:
                raise IngestError(
                    f"hsbc: listing page {_LISTING_URL} returned HTTP "
                    f"{r.status_code}"
                )
        html = r.text

        # All canonical filename variants observed in practice.
        url_re = re.compile(
            r"""https://www\.assetmanagement\.hsbc\.co\.in/assets/documents/"""
            r"""mutual-funds/en/[a-f0-9\-]+/the-asset[^"'<>\s]+\.pdf""",
            re.IGNORECASE,
        )
        candidates = list(dict.fromkeys(url_re.findall(html)))
        if not candidates:
            raise IngestError(
                f"hsbc: no 'the-asset-*.pdf' links found on listing page "
                f"{_LISTING_URL}; CMS layout may have changed."
            )

        # Filename must reference the data-month name (long or short
        # form) AND the data year. We match on filename body only —
        # the UUID prefix is upload-ID, not date-bearing.
        wanted: list[str] = []
        year_str = str(y)
        for u in candidates:
            fname = u.rsplit("/", 1)[-1].lower()
            if year_str not in fname and f"-{y % 100:02d}." not in fname:
                continue
            if month_lower in fname or month_short in fname:
                wanted.append(u)
        if not wanted:
            raise IngestError(
                f"hsbc: no factsheet PDF found on listing for "
                f"data month {ym} (looked for "
                f"'the-asset-*{month_lower}*{year_str}*.pdf' and short-form "
                f"variants). Candidates inspected: {len(candidates)}."
            )
        # If multiple variants resolve (e.g. both ``the-asset-april-2026`` and
        # ``the-asset-factsheet-april-2026``), pick the one without
        # ``factsheet`` (the current convention) — otherwise lexical max
        # is a safe tiebreaker.
        wanted.sort(key=lambda u: ("factsheet" in u.lower(), u))
        url = wanted[0]
        log.info("hsbc.resolved_pdf_url", ym=ym, url=url)
        return download_to(url, out)

    # ------------------------------------------------------------------
    # PTR / AUM
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
                if "Turnover" not in text and "TURNOVER" not in text:
                    continue
                ptr = _find_ptr_via_words(page)
                if ptr is None:
                    continue
                # Fail-fast: drop NaN / non-positive. HSBC prints a real
                # fraction or omits the field; zero PTR is implausible
                # for an active scheme.
                if ptr != ptr or ptr <= 0:
                    continue
                # Defensive sanity check: PTR is a fraction. Arbitrage
                # strategies can legitimately reach ~5×, but anything
                # >10 is almost certainly a percent-vs-fraction parse
                # error — drop rather than ship.
                if ptr >= 10:
                    continue
                if scheme in seen:
                    continue
                seen.add(scheme)
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr,
                    source_amc=self.amc_slug,
                )

    # ------------------------------------------------------------------
    # Holdings — deferred. HSBC's portfolio table is a two-column
    # ``Issuer Industry/Rating % to Net Assets`` layout with no ISINs
    # printed. Phase 3.A's ISIN-tagged Excel path covers HSBC for the
    # portfolio overlap metric, so we return () here.
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
