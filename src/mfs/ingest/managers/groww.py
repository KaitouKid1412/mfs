"""Groww Mutual Fund (formerly Indiabulls Mutual Fund) — factsheet adapter.

Calibrated against the April 2026 combined factsheet at
``data/raw/factsheets/groww/2026-04.pdf`` (~5.97 MB, 133 pages). The PDF
is served from the Groww netstorage CDN at

    https://assets-netstorage.growwmf.in/compliance_docs/Downloads/
        Fact%20Sheet/<fiscal_year>/Monthly%20Factsheet%20-%20<Month>%20<YYYY>.pdf

where ``<fiscal_year>`` is the Indian fiscal year folder ``YYYY - YYYY+1``
(April-March), i.e. April-2026 lives under ``2026 - 2027``. The folder
name uses spaces around the hyphen for FY 2025 onward (the 2024-25
archive uses ``2024- 2025`` with an irregular space, and 2023-24 uses
``2023-2024`` with no spaces — those are not relevant for ongoing
monthly ingest and we encode only the modern pattern).

URL discovery trail
-------------------
The Groww Mutual Fund site is at ``growwmf.in`` (the Groww broker portal
at groww.in/mutual-funds/amc/groww-mutual-fund 404s — that route is for
distributor pages, not the AMC's own factsheets). The factsheet listing
page lives at ``growwmf.in/downloads/factsheet`` and exposes a Next.js
SSR ``__NEXT_DATA__`` JSON payload that lists every published factsheet
PDF with its ``publicUrl`` on ``assets-netstorage.growwmf.in``. The
filename pattern is stable for FY 2025 onward; older releases use
abbreviated month names (e.g. ``Aug 25``, ``Dec 25``, ``Mar 2025``) and
would need a discovery step against the listing JSON. For 2026-04 the
canonical filename is ``Monthly Factsheet - April 2026.pdf`` (full month
name, four-digit year, single space dashes).

Layout findings driving the parser
----------------------------------
Groww's factsheet uses a unique TABULAR snapshot layout: pages 21-33
print every scheme as a column in a "Snapshot of <category>" table with
5-6 schemes per page. Each table row carries one metric:

    Row "Monthly Average AUM (Rs. in Crores)"  ` 129.17 Crore  ` 62.72 ...
    Row "Month End AUM (Rs. in Crores)"        ` 127.64 Crore  ` 66.85 ...
    Row "Portfolio Turnover"                   0.98            1.90    ...

This is far cleaner than the per-scheme-page approach used by other AMC
adapters — there's no column-bleed across schemes because the table is
laid out as a proper grid. We:

1. Locate the column anchors by clustering ``Groww`` word tokens in the
   top header band (y < 100) and picking the topmost cluster with >=2
   anchors (or single-anchor pages for single-scheme snapshots like the
   Money Market Fund page).
2. Read each column's scheme name from the title row (header_y ± a
   tight 14pt band — wider would bleed in the "Type of Scheme"
   description that immediately follows).
3. Locate the ``Month End AUM`` label-row y-anchor; read numeric tokens
   in that y-band and bucket them by column x.
4. Locate the ``Portfolio Turnover`` label-row y-anchor; same procedure.
   Debt-fund snapshot pages (32-33) don't print Portfolio Turnover and
   legitimately yield no PTR rows for those schemes.

PTR storage convention
----------------------
Groww prints PTR as a **fraction** directly (e.g. Large Cap = 0.98,
ELSS = 1.90, Arbitrage = 0.57), so no percent-to-fraction conversion is
needed. Drop NaN/zero/negative defensively.

AUM storage convention
----------------------
All AUM values are in INR Crore. We extract ``Month End AUM`` (point-in-
time, consistent with Stage 2's treatment of ``aum_crore``) rather than
``Monthly Average AUM``.

Per-scheme detail pages (34-106) also print these values but in a
non-tabular FUND SIZE block that would require column-bleed handling —
since the snapshot pages already cover every scheme, we don't bother.

Calibration counts on 2026-04
-----------------------------
* 13 snapshot pages (21-33), 57 scheme columns
* 57 AUM rows extracted (every column carries Month-End AUM)
* 51 PTR rows extracted (snapshot pages 32-33 are debt funds and
  legitimately omit the Portfolio Turnover row); 1 ETF prints
  ``Portfolio Turnover 0.00`` which fail-fast drops, leaving 50 PTR
  rows yielded by the adapter
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pdfplumber

from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)


_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _fiscal_year_folder(y: int, m: int) -> str:
    """Indian fiscal year folder (April-March) on the Groww CDN.

    A data month (YYYY-MM) with month >= 4 falls in FY ``Y - Y+1``;
    earlier months fall in ``Y-1 - Y``. The folder uses spaces around
    the hyphen for FY 2025 onward; the 2024-25 archive uses an
    irregular ``2024- 2025`` and 2023-24 uses ``2023-2024`` (no
    spaces). We only encode the modern (spaced) convention since
    ongoing ingest runs against the current fiscal year.
    """
    if m >= 4:
        fy_start = y
    else:
        fy_start = y - 1
    return f"{fy_start} - {fy_start + 1}"


def _is_snapshot_page(text: str) -> bool:
    """Return True iff the page's first non-empty line begins with
    ``"Snapshot"``.

    Snapshot pages (21-33 in the April 2026 issue) are the only pages
    that lay schemes out as a horizontal table. Per-scheme detail pages
    (34-106), the cover, TOC, CIO desk, market-outlook, IDCW history,
    riskometer, and disclosures pages all have other first-line text
    and must be skipped — otherwise the column anchor heuristic latches
    onto stray ``Groww`` tokens in body text (e.g. "investing in units
    of Groww Silver ETF" on the Silver-FOF detail page) and emits
    bogus rows.
    """
    if not text:
        return False
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        return s.lower().startswith("snapshot")
    return False


def _scheme_name_from_columns(
    page,
) -> tuple[list[tuple[float, float, float]], list[str], list[dict]] | None:
    """Locate snapshot-table column anchors on a Groww snapshot page.

    Returns ``(boundaries, names, all_words)`` where ``boundaries`` is a
    list of ``(left_x, right_x, header_y)`` tuples, ``names`` is the
    per-column scheme title, and ``all_words`` is the page's word list
    (re-used by the value-row extractors).

    Returns None for non-snapshot pages (cover, TOC, CIO desk, scheme
    detail pages, IDCW history, etc).
    """
    text = page.extract_text() or ""
    if not _is_snapshot_page(text):
        return None
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    # Header-band anchors: only ``Groww`` tokens whose top y < 100 (below
    # that, ``Groww`` appears inside the "Type of Scheme" description
    # paragraphs, not the table header).
    growws_all = [w for w in words if w["text"] == "Groww" and w["top"] < 100]
    if not growws_all:
        return None
    # Cluster anchors by y-band (within ~4pt) and pick the topmost band
    # that has >=2 anchors. Single-anchor bands at y < 70 are accepted
    # (single-scheme snapshot pages like the Money Market Fund page).
    growws_all.sort(key=lambda w: w["top"])
    bands: list[list[dict]] = []
    for g in growws_all:
        placed = False
        for b in bands:
            if abs(b[0]["top"] - g["top"]) < 4:
                b.append(g)
                placed = True
                break
        if not placed:
            bands.append([g])
    header_band: list[dict] | None = None
    for b in sorted(bands, key=lambda b: b[0]["top"]):
        if len(b) >= 2 or (len(b) == 1 and b[0]["top"] < 70):
            header_band = b
            break
    if header_band is None:
        return None
    growws = sorted(header_band, key=lambda w: w["x0"])
    header_y = min(g["top"] for g in growws)
    page_w = page.width
    boundaries: list[tuple[float, float, float]] = []
    names: list[str] = []
    for i, g in enumerate(growws):
        left = g["x0"] - 1
        right = growws[i + 1]["x0"] - 1 if i + 1 < len(growws) else page_w
        # Scheme name: header row spans roughly header_y ± a tight 14pt
        # band. Wider bleeds into the "Type of Scheme" description row
        # that starts ~18pt below; narrower clips wrapped titles like
        # "Groww Nifty EV & New Age Automotive ETF FOF" which wrap one
        # line.
        col_words = [
            w
            for w in words
            if left <= w["x0"] < right and header_y - 4 <= w["top"] <= header_y + 14
        ]
        col_words.sort(key=lambda w: (w["top"], w["x0"]))
        name = " ".join(w["text"] for w in col_words).strip()
        boundaries.append((left, right, header_y))
        names.append(name)
    return boundaries, names, words


def _find_row_anchor(
    words: list[dict], label_tokens: tuple[str, ...]
) -> dict | None:
    """Locate the y-position of a row whose label is the given
    space-separated token sequence (adjacent on the same y-band).

    Returns the *last* token of the label so callers can use its x1 as
    the left edge for the value scan.
    """
    n = len(words)
    first = label_tokens[0]
    rest = label_tokens[1:]
    for i in range(n):
        if words[i]["text"] != first:
            continue
        y = words[i]["top"]
        cur_x = words[i]["x1"]
        matched = [words[i]]
        ok = True
        for tok in rest:
            found = None
            for v in words:
                if v["text"] != tok:
                    continue
                if abs(v["top"] - y) > 4:
                    continue
                if cur_x < v["x0"] < cur_x + 40:
                    found = v
                    break
            if found is None:
                ok = False
                break
            cur_x = found["x1"]
            matched.append(found)
        if ok:
            return matched[-1]
    return None


_NUMERIC_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _extract_row_values(
    words: list[dict],
    label_last_word: dict,
    boundaries: list[tuple[float, float, float]],
) -> list[str | None]:
    """Collect numeric tokens within ±6pt of the label row, to the right
    of the label's last word, and bucket them by column x-range.

    Returns a list of cleaned numeric strings (commas removed), one per
    column, or None if no value fell in that column. Wrap-tolerant: the
    snapshot tables sometimes split a value row across two visual lines
    (the leading currency-glyph backtick on one y, the digits on the
    next), so we accept a generous y-tolerance.
    """
    label_y = label_last_word["top"]
    label_x1 = label_last_word["x1"]
    candidates: list[tuple[float, str]] = []
    for w in words:
        if abs(w["top"] - label_y) > 6:
            continue
        if w["x0"] <= label_x1:
            continue
        cleaned = w["text"].replace(",", "")
        if _NUMERIC_RE.match(cleaned):
            candidates.append((w["x0"], cleaned))
    col_values: list[str | None] = [None] * len(boundaries)
    for x0, val in candidates:
        for i, (left, right, _) in enumerate(boundaries):
            # Allow a 5pt slack at the left edge — value tokens are
            # right-aligned within the column and their x0 can land
            # slightly before the column's nominal left boundary
            # (column boundaries are defined by header-text x0).
            if left - 5 <= x0 < right:
                col_values[i] = val
                break
    return col_values


def _clean_scheme_name(raw: str) -> str | None:
    """Normalise whitespace and drop empty results."""
    if not raw:
        return None
    s = re.sub(r"\s+", " ", raw).strip()
    if not s:
        return None
    # Defensive: must start with the Groww brand to be a real scheme
    # title (the column extractor already guarantees this since columns
    # are anchored on ``Groww`` tokens).
    if not s.lower().startswith("groww"):
        return None
    return s


@register_adapter
class GrowwAdapter(ManagerAdapter):
    """Groww Mutual Fund factsheet adapter.

    ``amc_slug = 'groww'`` matches ``scheme_master.amc_code`` directly,
    so no alias entry in ``_scheme_match.py`` is required.

    Groww Mutual Fund is the former Indiabulls Mutual Fund, rebranded
    after the Groww acquisition in 2023. Pre-rebrand factsheets used the
    ``Indiabulls `` prefix and are not parsed by this adapter (the
    column anchor would not match); current AMFI scheme names retain the
    legacy ``(formerly known as Indiabulls ...)`` parenthetical, which
    the fuzzy matcher tolerates via token_set_ratio.
    """

    amc_slug = "groww"
    source_label = "Groww Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical Groww combined-factsheet PDF URL for
        data month ``ym='YYYY-MM'``.

        The Groww CDN lives at ``assets-netstorage.growwmf.in`` and
        organises monthly factsheets under Indian fiscal-year folders
        (April-March). Modern (FY-2025 onward) filenames use the full
        month name and four-digit year (``Monthly Factsheet - April
        2026.pdf``); older months were uploaded with abbreviated names
        and would require a discovery pass against the
        ``/downloads/factsheet`` Next.js JSON listing. We encode only
        the modern pattern since ongoing ingest runs against the
        current fiscal year.
        """
        y, m = map(int, ym.split("-"))
        fy = _fiscal_year_folder(y, m)
        month_name = _MONTH_NAMES[m - 1]
        filename = f"Monthly Factsheet - {month_name} {y}.pdf"
        # URL-encode spaces only; the ``%20`` encoding is what the CDN
        # actually serves (the listing JSON returns this form).
        encoded_fy = fy.replace(" ", "%20")
        encoded_filename = filename.replace(" ", "%20")
        return (
            "https://assets-netstorage.growwmf.in/compliance_docs/Downloads/"
            f"Fact%20Sheet/{encoded_fy}/{encoded_filename}"
        )

    # ------------------------------------------------------------------
    # PTR / AUM (single-pass over snapshot pages)
    # ------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                result = _scheme_name_from_columns(page)
                if result is None:
                    continue
                boundaries, names, words = result
                ptr_label = _find_row_anchor(words, ("Portfolio", "Turnover"))
                if ptr_label is None:
                    # Debt-fund snapshot pages legitimately don't print
                    # Portfolio Turnover; skip.
                    continue
                values = _extract_row_values(words, ptr_label, boundaries)
                for name, val in zip(names, values):
                    if val is None:
                        continue
                    clean_name = _clean_scheme_name(name)
                    if not clean_name:
                        continue
                    try:
                        ptr = float(val)
                    except ValueError:
                        continue
                    # Fail-fast: drop NaN, zero, or negative PTR. Groww
                    # prints a real fraction or omits the row; a value
                    # of 0.00 (Nifty 1D Rate Liquid ETF) is a true zero
                    # for an overnight-style ETF but we treat it the
                    # same as missing for the active-management metric
                    # set.
                    if ptr != ptr or ptr <= 0:
                        continue
                    yield ParsedPtrRecord(
                        scheme_name_printed=clean_name,
                        ptr=ptr,
                        source_amc=self.amc_slug,
                    )

    # ------------------------------------------------------------------
    # Holdings — deferred. The per-scheme detail pages (34-106) carry a
    # holdings table without ISINs; Phase 3.C provides the parallel
    # ISIN-tagged Excel path for portfolio overlap. We return () here.
    # ------------------------------------------------------------------

    def parse_holdings(
        self, pdf_path: Path, ym: str
    ) -> Iterable[ParsedHoldingRecord]:
        return ()
