"""Mirae Asset Mutual Fund — factsheet adapter.

Calibrated against the April 2026 publish (data month March 31, 2026) of the
combined Mirae factsheet (~7.8 MB, 90 pages, cached at
``data/raw/factsheets/mirae/2026-04.pdf``).

Layout (per-scheme detail pages start at the equity section, page ~25):
- Each detail page begins with a two-line header: line 1 is ``MIRAE ASSET`` and
  line 2 is the scheme name in upper case, e.g. ``LARGE CAP FUND``. One scheme
  (Infrastructure Fund) inserts a ``$`` footnote marker between the two lines,
  so we skip pure-symbol lines when assembling the printed scheme name.
- PTR shows up in two visual layouts:
    1. Most equity pages: ``Portfolio Turnover Ratio 0.36 times`` on one
       line (regex catches it directly).
    2. Hybrid / arbitrage pages: the "Portfolio Turnover Ratio" label wraps
       across three lines AND the right-side portfolio column bleeds into
       the same y-band, so the value appears as a standalone row like
       ``0.93 Times``. We fall back to word-position extraction confined to
       the left column (x < 280) to recover the value.
  PTR is printed as a **fraction** (e.g. ``0.36 times`` → store as 0.36; an
  arbitrage fund's ``16.99 times`` → store as 16.99). No percent → fraction
  conversion is needed.
- Holdings: Mirae factsheets do not print ISINs in the portfolio table, so
  the orchestrator would drop every row at the ISIN gate. We therefore leave
  ``parse_holdings`` at the base class no-op default; ISIN-tagged holdings
  come from the parallel Excel-based ingestion path (Phase 3.C).

URL pattern (Sitefinity CMS, per the public listing at
https://www.miraeassetmf.co.in/downloads/factsheet):
  https://www.miraeassetmf.co.in/docs/default-source/fachsheet/
    factsheet-{publish-month-lower}-{publish-year}.pdf
The site URL the CMS resolves at runtime may carry a ``?sfvrsn=`` version
token; we encode the canonical path without it. ``publish_ym`` is data
month + 1 (Mirae publishes by the 10th of the next month, matching HDFC/ABSL
convention). Note the directory misspelling ``fachsheet`` is intentional —
it's how Mirae has hosted these PDFs for years.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pdfplumber

from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)


_MONTH_FULL = (
    "January|February|March|April|May|June|"
    "July|August|September|October|November|December"
)
_MONTH_NAMES_LIST = _MONTH_FULL.split("|")


def _publish_ym(data_ym: str) -> str:
    """data month YYYY-MM → publish month YYYY-MM (data + 1 with year roll)."""
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{m + 1:02d}"


# Scheme-name footnote markers (Infrastructure Fund prints '$' between the
# 'MIRAE ASSET' header and the actual scheme line). These are filtered out
# when assembling the printed name from line tokens.
_NAME_FOOTNOTE_MARKERS = {"$", "*", "#", "@", "^", "†", "‡"}
_NAME_TERMINATOR_RE = re.compile(r"\b(FUND|PLAN|FOF)\b", re.IGNORECASE)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name if this is a per-scheme detail page.

    Detail pages always start with a line containing exactly ``MIRAE ASSET``,
    followed by the scheme name on the next 1-2 lines (uppercase). Cover,
    glossary, snapshot, and SID pages don't have this header so they return
    None and are skipped by the orchestrator.
    """
    if not text:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "MIRAE ASSET":
        return None
    tokens = ["MIRAE ASSET"]
    # Look at the next few lines for the scheme name. Stop when we hit a
    # FUND/PLAN/FOF token (end of name) or a paren-description line.
    for ln in lines[1:6]:
        s = ln.strip()
        if not s:
            continue
        if s in _NAME_FOOTNOTE_MARKERS:
            continue
        if s.startswith("("):
            break
        tokens.append(s)
        if _NAME_TERMINATOR_RE.search(s):
            break
    name = " ".join(tokens)
    # Title-case so the fuzzy matcher treats it identically to scheme_master
    # entries (canonicalize() upper-cases both sides anyway, but a stable
    # representation helps logs).
    return name.title()


# PTR regex (clean text layout): "Portfolio Turnover Ratio 0.36 times" or
# "Portfolio Turnover 1.01 times" (some pages drop the "Ratio" word).
_MIRAE_PTR_TEXT_RE = re.compile(
    r"Portfolio\s+Turnover(?:\s+Ratio)?\s+(\d+\.\d+)\s*[Tt]imes",
    re.IGNORECASE,
)


def _ptr_from_words(words: list[dict]) -> float | None:
    """Recover the PTR value when text extraction fails.

    Several hybrid pages (Aggressive Hybrid, Equity Savings, Arbitrage,
    Balanced Advantage, Multi Asset Allocation) have the right-side portfolio
    column bleeding into the same y-bands as the "Portfolio Turnover Ratio"
    label, leaving the value on a standalone row like ``0.93 Times``. We
    locate any ``Times`` token in the left column band (x0 < 280) that sits
    near a ``Turnover`` label (within ~25pt vertically) and read the numeric
    word immediately to its left.
    """
    for w in words:
        if w["text"].lower() != "times":
            continue
        if w["x0"] > 280.0:
            continue
        # Find a numeric word in the same y-band, immediately to the left.
        numeric_val = None
        for v in words:
            if v is w:
                continue
            if abs(v["top"] - w["top"]) > 3:
                continue
            if v["x0"] >= w["x0"]:
                continue
            try:
                numeric_val = float(v["text"])
            except ValueError:
                continue
        if numeric_val is None:
            continue
        # Confirm a 'Turnover' label is nearby vertically (within ~25pt) in
        # the same left band — this guards against unrelated 'N.NN Times'
        # tokens elsewhere on the page.
        has_turnover_label = any(
            t["text"] == "Turnover"
            and abs(t["top"] - w["top"]) < 25
            and t["x0"] < 280.0
            for t in words
        )
        if not has_turnover_label:
            continue
        return numeric_val
    return None


@register_adapter
class MiraeAdapter(ManagerAdapter):
    """Mirae Asset Mutual Fund factsheet adapter."""

    amc_slug = "mirae"
    source_label = "Mirae Asset Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Build the canonical PDF URL for data month ym='YYYY-MM'.

        Mirae names the file by the PUBLISH month, not the data month —
        e.g. the file published in April 2026 covers data through March 31,
        2026. We shift ym forward by one month before forming the URL.
        """
        publish_ym = _publish_ym(ym)
        pub_y, pub_m = map(int, publish_ym.split("-"))
        month_lower = _MONTH_NAMES_LIST[pub_m - 1].lower()
        return (
            "https://www.miraeassetmf.co.in/docs/default-source/fachsheet/"
            f"factsheet-{month_lower}-{pub_y}.pdf"
        )

    # -----------------------------------------------------------------
    # PTR extraction
    # -----------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                # Try the clean text regex first; fall back to word
                # positions for pages where column-bleed breaks the line.
                m = _MIRAE_PTR_TEXT_RE.search(text)
                if m:
                    try:
                        ptr_val = float(m.group(1))
                    except ValueError:
                        continue
                else:
                    words = page.extract_words(use_text_flow=True)
                    ptr_val = _ptr_from_words(words)
                if ptr_val is None:
                    continue
                # Sanity: drop NaN / non-positive values (PTR is always > 0
                # if reported — debt schemes simply omit it).
                if ptr_val != ptr_val or ptr_val <= 0:  # NaN check
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_val,
                    source_amc=self.amc_slug,
                )
