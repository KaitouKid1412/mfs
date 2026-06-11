"""Quantum Mutual Fund — factsheet PTR adapter (Phase 3).

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/quantum/2026-04.pdf``, ~4.6 MB, 44 pages). The cover
reads "Monthly Update of our Mutual Fund Schemes / Factsheet – April'26" and
every scheme page is dated "as on April 30, 2026".

URL discovery
-------------
Quantum serves every monthly factsheet from an opaque GUID URL
(``/FileCDN/FactSheet/<uuid>.pdf``) — there is NO date-derivable filename. The
stable, slug-stable entry point is the combined-factsheet listing endpoint
``/factsheets/combined/-1/0/0``, a server-rendered HTML page whose ``<a>`` link
text labels each PDF with its **data month** ("April 2026 - All Funds",
"March 2026 - All Funds", ...). ``build_url(ym)`` fetches that listing and
returns the GUID URL whose label matches the requested data month. Because the
label is keyed on the DATA month (not the publish month), this transparently
absorbs Quantum's usual one-month publish lag without a ``_publish_ym`` shift.

Layout findings driving the PTR parser
--------------------------------------
* PTR is labeled **"Portfolio Turnover Ratio"** (the equity/hybrid pages add a
  "(Last one year)" qualifier). The five active-equity scheme pages are:
  Value (p15), Small Cap (p16), Ethical (p17), ELSS Tax Saver (p18),
  ESG Best In Class Strategy (p19).

* UNIT — Quantum prints the PTR as a **percent** value. On most equity pages
  the literal "%" is omitted (e.g. Value Fund prints a bare ``12.29`` meaning
  12.29% turnover → 0.1229 fraction); the ETF / hybrid pages print it with the
  sign (``22.54%``, ``2.38%``). In EVERY case it is a percent, so we divide by
  100 to get the stored fraction. A bare ``12.29`` is NOT already a fraction —
  treating it as one would imply 1229% turnover, absurd for Quantum's
  low-churn value philosophy. (Storage convention: 1.27 == 127%.)

* The value is laid out two ways depending on the page template:
    - **Column-box** (Value / ELSS / ESG): a stats strip prints the value row
      ABOVE a header row of "Standard Deviation | Beta | Sharpe Ratio |
      Portfolio Turnover Ratio". The PTR number is column-aligned under the
      "Turnover" word, one text line up.
    - **Below-label** (Small Cap / Ethical): "Portfolio Turnover Ratio (Last
      one year)" sits in the sector-allocation band with the value on the
      next line directly below.
    - **Inline** (Multi Asset Allocation, a hybrid): the value is on the same
      line — "Equity Portfolio Turnover Ratio (Last one year): 2.38%" plus a
      separate "Total Portfolio Turnover Ratio ... : 136.52%". We prefer the
      Equity figure and explicitly reject the Total one.

* Two-strategy extraction:
    1. **Text regex** — catches the inline ``: X%`` form, preferring the
       ``Equity`` PTR over the ``Total`` PTR.
    2. **Word-position fallback** — finds the "Portfolio Turnover" label
       (rejecting "Total Portfolio Turnover"), then the nearest numeric token
       that is vertically adjacent (±32pt, not the label's own line) AND
       column-aligned with the label (x within roughly [-55, +75] of the
       "Turnover" word). Handles both the column-box and below-label templates.

Calibration on 2026-04: 7 PTR rows extracted (Value 12.29%, Small Cap 11.06%,
Ethical 9.73%, ELSS 10.66%, ESG 27.51%, Nifty 50 ETF 22.54%, and Multi Asset
Allocation 2.38% via the inline Equity figure). The 5 ranked active-equity
funds are all covered. Pure debt / liquid / gold / FoF pages legitimately omit
PTR and are dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pdfplumber

from mfs.errors import IngestError
from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.io.http import fetch_bytes
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Combined-factsheet listing endpoint (slug-stable). Each PDF link is labeled
# by its DATA month, e.g. "April 2026 - All Funds".
_LISTING_URL = "https://www.quantumamc.com/factsheets/combined/-1/0/0"

# <a href="...FileCDN/FactSheet/<uuid>.pdf" ...>...<text>...</a>
_LINK_RE = re.compile(
    r'href="([^"]*FileCDN/FactSheet/[a-fA-F0-9-]+\.pdf)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _data_month_label(ym: str) -> str:
    """data month YYYY-MM -> 'MonthName Year' (e.g. '2026-04' -> 'April 2026')."""
    y, m = ym.split("-")
    return f"{_MONTH_NAMES[int(m) - 1]} {int(y)}"


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Inline form (hybrid pages only): "Equity Portfolio Turnover Ratio (Last one
# year): 2.38%". Quantum prints PTR as a PERCENT, so the captured number is
# divided by 100 to store a fraction. We REQUIRE the colon-delimited value to
# sit immediately after the optional "(Last one year)" qualifier — on the
# bare-number equity pages extract_text column-bleeds a holding weight after
# the label, so a looser pattern would mis-grab e.g. "...Ltd 2.45%". Those
# bare-number pages fall through to the word-position strategy instead.
# `(?<!Total )` rejects the "Total Portfolio Turnover Ratio ..." sum line.
_PTR_INLINE_RE = re.compile(
    r"(?<!Total )Portfolio\s+Turnover\s+Ratio\s*"
    r"(?:\(Last\s+one\s+year\))?\s*:\s*"
    r"(\d{1,3}(?:\.\d{1,2})?)\s*%",
    re.IGNORECASE,
)

# A bare or percent-suffixed number with at most 2 decimals; the value column
# tokens look like "12.29", "11.06", "22.54%".
_NUM_RE = re.compile(r"^\d{1,3}(?:\.\d{1,2})?%?$")


def _find_ptr_via_words(words: list[dict]) -> float | None:
    """Word-position fallback for the column-box / below-label templates.

    Locate a "Portfolio Turnover" label (Portfolio immediately left of
    Turnover on the same y-band, and NOT preceded by "Total"), then take the
    nearest numeric token that is on a different text line (|dy| >= 3) within
    ±32pt vertically and column-aligned with the label
    (x in roughly [label_x - 55, label_x + 75]).

    Returns the raw PERCENT value (caller divides by 100) or None.
    """
    for w in words:
        if w["text"] != "Turnover":
            continue
        # "Portfolio" must sit just left of "Turnover" on the same line.
        portfolio = next(
            (
                v for v in words
                if v["text"] == "Portfolio"
                and abs(v["bottom"] - w["bottom"]) <= 3
                and 0 < (w["x0"] - v["x1"]) < 30
            ),
            None,
        )
        if portfolio is None:
            continue
        # Reject "Total Portfolio Turnover" (the Equity+Debt+ETF sum).
        is_total = any(
            v.get("text") == "Total"
            and abs(v["bottom"] - portfolio["bottom"]) <= 3
            and 0 < (portfolio["x0"] - v["x1"]) < 30
            for v in words
        )
        if is_total:
            continue

        lx0 = w["x0"]
        ly = w["bottom"]
        candidates: list[tuple[float, dict]] = []
        for v in words:
            t = v["text"].rstrip(",;.")
            if not _NUM_RE.match(t):
                continue
            dy = abs(v["bottom"] - ly)
            if dy < 3:  # same text line as the label — skip
                continue
            if dy > 32:
                continue
            if not (-55 <= (v["x0"] - lx0) <= 75):
                continue
            candidates.append((dy, v))
        candidates.sort(key=lambda c: c[0])
        for _, v in candidates:
            txt = v["text"].rstrip(",;.").rstrip("%")
            try:
                val = float(txt)
            except ValueError:
                continue
            if val > 0:
                return val
    return None


@register_adapter
class QuantumAdapter(ManagerAdapter):
    """Quantum Mutual Fund factsheet adapter (PTR only; holdings via Excel path)."""

    amc_slug = "quantum"
    source_label = "Quantum Mutual Fund"

    # -------------------------------------------------------------------
    # URL
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Resolve the combined-factsheet PDF URL for data month ym='YYYY-MM'.

        Quantum uses opaque GUID filenames, so we scrape the slug-stable
        listing endpoint and match the data-month label. Raises IngestError
        if the listing can't be fetched or the month isn't published yet.
        """
        want = _data_month_label(ym)
        try:
            html = fetch_bytes(_LISTING_URL).decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            raise IngestError(
                f"quantum: could not fetch factsheet listing {_LISTING_URL}: {e}"
            ) from e
        for href, inner in _LINK_RE.findall(html):
            label = re.sub(r"\s+", " ", _TAG_RE.sub(" ", inner)).strip()
            # Label looks like "April 2026 - All Funds".
            if label.lower().startswith(want.lower()):
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = "https://www.quantumamc.com" + href
                return href
        raise IngestError(
            f"quantum: no combined factsheet labeled {want!r} found at "
            f"{_LISTING_URL} (not published yet?)"
        )

    # -------------------------------------------------------------------
    # PTR
    # -------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if "urnover" not in text.lower():
                    continue
                scheme = self._scheme_name_from_page(text)
                if not scheme:
                    continue

                ptr_value: float | None = None

                # Strategy 1: inline "...: X%" (prefer Equity over Total — the
                # regex already excludes the Total line).
                m = _PTR_INLINE_RE.search(text)
                if m:
                    try:
                        ptr_value = float(m.group(1))
                    except ValueError:
                        ptr_value = None

                # Strategy 2: column-aligned / below-label bare number.
                if ptr_value is None:
                    ptr_value = _find_ptr_via_words(
                        page.extract_words(use_text_flow=True)
                    )

                if ptr_value is None:
                    continue
                # Fail-fast: drop NaN / non-positive — a real scheme prints a
                # positive percent or omits the block entirely.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue

                # Quantum prints PTR as a percent -> store as fraction.
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value / 100.0,
                    source_amc=self.amc_slug,
                )

    # -------------------------------------------------------------------
    # Scheme-name detection
    # -------------------------------------------------------------------

    @staticmethod
    def _scheme_name_from_page(text: str) -> str | None:
        """Return the printed scheme name (first non-empty line) for a scheme
        page, or None for non-scheme pages (cover, outlook, SIP/performance
        tables, taxation, contact).

        Quantum scheme pages begin with the banner ``Quantum <Fund Name>``.
        """
        if not text:
            return None
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return None
        first = re.sub(r"\s+", " ", lines[0]).strip()
        if not first.startswith("Quantum "):
            return None
        # Guard against two scheme banners bleeding onto one line.
        if first.count("Quantum ") > 1:
            return None
        return first or None

    # -------------------------------------------------------------------
    # Holdings — handled by the separate Excel-based path; default no-op.
    # -------------------------------------------------------------------

    def parse_holdings(
        self, pdf_path: Path, ym: str
    ) -> Iterable[ParsedHoldingRecord]:
        return ()
