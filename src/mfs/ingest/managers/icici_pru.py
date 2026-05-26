"""ICICI Prudential Mutual Fund — manager-tenure adapter.

Calibrated against the April 2026 factsheet:
  https://www.icicipruamc.com/blob/downloads/Files/Historic%20Factsheets/2025-2026/Complete%20Factsheet%20April%202026.pdf
  (~12MB, 166 pages — URL discovered via the Wayback Machine after the
  homepage's JS-rendered SPA hid the live link.)

The 2026-05-24 deferral has been reversed (PR 2.1.F revisits ICICI Pru). The
SPA itself is still inaccessible without Playwright, but the underlying CDN
URL is stable across months when you know the pattern.

Layout (a third distinct style from HDFC/SBI/Nippon/ABSL):
- Per-scheme page first line is `"ICICI Prudential <X> Fund"`.
- Manager information is *narrative text*, not a table or labeled block:
    "The scheme is currently managed by Sankaran Naren, Vaibhav Dusad and
     Sharmila D'silva. Mr. Sankaran Naren has been managing this fund since
     Feb 2026. ... Mr. Vaibhav Dusad has been managing this fund since
     Jan 2021. ..."
- Two verb phrasings coexist within the same document:
    1. `<Name> has been managing this fund since <Month> <Year>`
    2. `<Name> currently manages the scheme since <Month> <Year>`
  Both supported.
- Date format: `<Month> [DD,]? <Year>` — abbreviated OR full month name,
  comma optional. Day usually absent → default day=1.
- Honorifics ("Mr./Ms.") are PRESENT on some pages and ABSENT on others.
  Inconsistent. The regex makes them optional.
- Apostrophes in names ("D'silva") need to be allowed.
- No lead/co labels → picker falls back to "earliest" (matches HDFC/SBI/ABSL).
- pdfplumber column-bleed can prepend a category label ("Concentrated",
  "Diversified", etc.) to the captured name when the previous text line
  is a one-word category banner. Post-processing trims the name to the
  last 2 tokens when no honorific anchors the match.

URL pattern: organized by Indian fiscal year folder. April 2026 data lives
in `/Historic Factsheets/2025-2026/` (technically the previous FY folder —
ICICI Pru appears to keep early-month files in the previous FY directory
until they migrate them). build_url tries both candidate FY directories on
fetch and uses whichever responds 200.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Iterable

import pdfplumber

from mfs.errors import IngestError
from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_MONTH_FULL = (
    "January|February|March|April|May|June|"
    "July|August|September|October|November|December"
)
_MONTH_ABBR = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"

_MONTH_INDEX = {
    name.lower(): i + 1 for i, name in enumerate(_MONTH_FULL.split("|"))
}
_MONTH_ABBR_INDEX = {
    name.lower(): i + 1 for i, name in enumerate(_MONTH_ABBR.split("|"))
}

# Sentence-anchored regex: name must follow a period+space (or newline+spaces)
# so we don't bleed across sentence boundaries into prior text. Captures both
# verb phrasings.
_MGR_NARRATIVE_RE = re.compile(
    rf"""
    (?:[.\n]\s*)                                                # sentence boundary
    (?P<name>
        (?:(?:Mr|Ms|Mrs|Dr)\.?\s+)?                             # optional honorific
        [A-Z][a-zA-Z'-]+                                        # first token
        (?:\s+[A-Z][a-zA-Z'-]+){{0,3}}                          # 0-3 more tokens
    )
    \s+
    (?:has\s+been\s+managing\s+(?:this|the)\s+(?:fund|scheme)   # variant 1
       |currently\s+manages?\s+(?:this|the)\s+(?:fund|scheme))  # variant 2
    \s+since\s+
    (?P<month>{_MONTH_FULL}|{_MONTH_ABBR})
    ,?\s+
    (?P<year>\d{{4}})
    """,
    re.IGNORECASE | re.VERBOSE,
)

_HONORIFIC_RE = re.compile(r"^(Mr|Ms|Mrs|Dr)\.?\s+", re.IGNORECASE)
_BANNED_PREFIXES = {
    # Category / style banners pdfplumber occasionally fuses onto the next
    # sentence. If the matched name starts with one of these and lacks an
    # honorific, the prefix is post-trimmed.
    "concentrated", "diversified", "thematic", "sectoral", "passive",
    "equity", "debt", "hybrid", "balanced", "value", "contra", "focused",
    "midcap", "smallcap", "largecap", "multicap", "flexicap",
}


def _clean_manager_name(raw: str) -> str:
    """Strip honorific, prune leading category banners, normalize spacing."""
    s = raw.strip()
    s = _HONORIFIC_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return s
    # If the first token is a known category banner and the name has >2 tokens,
    # drop tokens from the left until either the first token is no longer a
    # banner or only the last 2 tokens remain.
    tokens = s.split()
    while len(tokens) > 2 and tokens[0].lower() in _BANNED_PREFIXES:
        tokens = tokens[1:]
    return " ".join(tokens)


def _parse_my(month_str: str, year_str: str) -> date | None:
    key = month_str.lower()
    m = _MONTH_INDEX.get(key) or _MONTH_ABBR_INDEX.get(key)
    if not m:
        return None
    try:
        return date(int(year_str), m, 1)
    except ValueError:
        return None


def _fy_dir_candidates(ym: str) -> list[str]:
    """Return the FY directory candidates for the given data month.

    ICICI Pru organizes factsheets by FY folder, but during transition (April
    of a new FY) they keep files in the *previous* FY folder for a while. We
    try both the proper FY (year/year+1) and the previous-FY (year-1/year),
    in order, and return the first that responds.
    """
    y, m = map(int, ym.split("-"))
    # Indian FY: April Y to March Y+1 = FY "Y-Y+1"
    if m >= 4:
        proper_fy = f"{y}-{y + 1}"
        prev_fy = f"{y - 1}-{y}"
    else:
        proper_fy = f"{y - 1}-{y}"
        prev_fy = f"{y - 2}-{y - 1}"
    return [prev_fy, proper_fy]


@register_adapter
class IciciPruAdapter(ManagerAdapter):
    """ICICI Prudential Mutual Fund factsheet adapter."""

    amc_slug = "icici_pru"
    source_label = "ICICI Prudential Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical-best-guess URL. fetch() tries fallbacks."""
        y, _ = map(int, ym.split("-"))
        month_name = _MONTH_FULL.split("|")[int(ym.split("-")[1]) - 1]
        fy = _fy_dir_candidates(ym)[0]
        return (
            "https://www.icicipruamc.com/blob/downloads/Files/Historic%20Factsheets/"
            f"{fy}/Complete%20Factsheet%20{month_name}%20{y}.pdf"
        )

    def fetch(self, ym: str) -> Path:
        """Try each FY candidate in turn; first 200-OK wins."""
        from mfs import paths
        from mfs.io.http import fetch_bytes

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        y, m = map(int, ym.split("-"))
        month_name = _MONTH_FULL.split("|")[m - 1]
        last_err: Exception | None = None
        for fy in _fy_dir_candidates(ym):
            url = (
                "https://www.icicipruamc.com/blob/downloads/Files/"
                f"Historic%20Factsheets/{fy}/"
                f"Complete%20Factsheet%20{month_name}%20{y}.pdf"
            )
            try:
                data = fetch_bytes(url)
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.info("managers.icici_pru.fetch_miss", fy=fy, err=str(e))
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(out.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.rename(out)
            log.info("managers.icici_pru.fetch_ok", fy=fy, bytes=len(data))
            return out
        raise IngestError(
            f"ICICI Pru: could not fetch April {y} factsheet from any FY "
            f"directory candidate. Last error: {last_err}"
        )

    @staticmethod
    def _scheme_name_from_page(text: str) -> str | None:
        if not text:
            return None
        first = text.splitlines()[0].strip()
        # Match "ICICI Prudential <something> Fund"
        if not first.startswith("ICICI Prudential "):
            return None
        # Ensure it ends with "Fund" or "Plan" — guard against TOC pages that
        # also start with "ICICI Prudential".
        if not re.search(r"\b(Fund|Plan)\b", first):
            return None
        return first

    # -------------------------------------------------------------------
    # Holdings extraction (Phase 2.2.D)
    # -------------------------------------------------------------------

    # ICICI Pru portfolio layout: two columns plus a far-right metadata column.
    # Left col: x ≈ 35–175 (name) + x ≈ 158–175 (weight column starts here).
    # Right col: x ≈ 215–360. Anything x > 400 is stats / disclaimers.
    _ICICI_LEFT_NAME = (35.0, 158.0)
    _ICICI_LEFT_WEIGHT = (155.0, 200.0)
    _ICICI_RIGHT_NAME = (210.0, 335.0)
    _ICICI_RIGHT_WEIGHT = (332.0, 360.0)
    _ICICI_STOP_TOKENS = {
        "Treasury", "Government", "Convertible", "Cash,", "Total",
        "Preference", "Net", "Grand",
    }
    # Tokens that, if PRESENT in a candidate name, signal it's a real equity
    # holding rather than a sector aggregate. ICICI Pru's portfolio groups
    # stocks under sector headers like "Banks 22.31%" — those have no Ltd./Inc.
    # suffix. Suffix detection only (case-insensitive, end-of-name); "Bank"
    # by itself doesn't qualify because "Banks" is a sector banner.
    _STOCK_SUFFIX_RE = re.compile(
        r"\b(Ltd|Limited|Inc|Corp|Co\.|PLC|S\.A\.)\b\.?$", re.IGNORECASE
    )

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                yield from self._parse_page_holdings(page, page_idx + 1)

    def _parse_page_holdings(self, page, page_num: int) -> Iterable[ParsedHoldingRecord]:
        text = page.extract_text() or ""
        scheme_name = self._scheme_name_from_page(text)
        if not scheme_name:
            return
        try:
            words = page.extract_words(use_text_flow=True)
        except Exception:  # noqa: BLE001
            return
        eq_y = self._find_equity_shares_y(words)
        if eq_y is None:
            return

        # Find the y at which the equity section ends.
        stop_y = self._find_section_end_y(words, eq_y)

        # Group rows in the portfolio area.
        band = [
            w for w in words
            if eq_y + 3 < w["bottom"] < (stop_y if stop_y else eq_y + 600)
            and w["x0"] < 400
        ]
        band.sort(key=lambda w: (w["bottom"], w["x0"]))
        rows: list[list[dict]] = []
        for w in band:
            if rows and abs(w["bottom"] - rows[-1][-1]["bottom"]) < 3:
                rows[-1].append(w)
            else:
                rows.append([w])

        for row in rows:
            for col_range, weight_range in (
                (self._ICICI_LEFT_NAME, self._ICICI_LEFT_WEIGHT),
                (self._ICICI_RIGHT_NAME, self._ICICI_RIGHT_WEIGHT),
            ):
                hold = self._extract_icici_column(row, col_range, weight_range)
                if hold is None:
                    continue
                # Skip sector aggregates: they lack typical equity-name tokens.
                if not self._STOCK_SUFFIX_RE.search(hold["name"]):
                    continue
                # Reject mangled names: more than one "Ltd." or absurd length.
                if hold["name"].count("Ltd.") + hold["name"].count("Ltd ") > 1:
                    continue
                if len(hold["name"].split()) > 7:
                    continue
                yield ParsedHoldingRecord(
                    scheme_name_printed=scheme_name,
                    security_name=hold["name"],
                    weight_pct=hold["weight"],
                    isin=None,
                    instrument_type="Equity",
                    source_amc=self.amc_slug,
                )

    @staticmethod
    def _find_equity_shares_y(words: list[dict]) -> float | None:
        for w in words:
            if w["text"] != "Equity":
                continue
            for v in words:
                if (
                    v["text"] == "Shares"
                    and abs(v["bottom"] - w["bottom"]) < 3
                    and v["x0"] > w["x1"]
                ):
                    return w["bottom"]
        return None

    @classmethod
    def _find_section_end_y(cls, words: list[dict], eq_y: float) -> float | None:
        for w in words:
            if w["bottom"] < eq_y + 5:
                continue
            if w["text"] in cls._ICICI_STOP_TOKENS and w["x0"] < 200:
                return w["bottom"]
        return None

    # -------------------------------------------------------------------
    # PTR extraction (Phase 2.2.H)
    # ICICI Pru: "Equity - 0.78 times" — value already in fraction form.
    # -------------------------------------------------------------------

    _ICICI_PTR_RE = re.compile(r"Equity\s*[-–]\s*(\d+\.\d+)\s*times", re.IGNORECASE)

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = self._scheme_name_from_page(text)
                if not scheme:
                    continue
                m = self._ICICI_PTR_RE.search(text)
                if not m:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=float(m.group(1)),
                    source_amc=self.amc_slug,
                )

    @staticmethod
    def _extract_icici_column(
        row: list[dict],
        name_range: tuple[float, float],
        weight_range: tuple[float, float],
    ) -> dict | None:
        name_words = [
            w for w in row
            if name_range[0] <= w["x0"] <= name_range[1]
            and w["text"] not in {"•", "@"}
        ]
        weight_words = [
            w for w in row
            if weight_range[0] <= w["x0"] <= weight_range[1]
        ]
        if not name_words or not weight_words:
            return None
        name_words.sort(key=lambda w: w["x0"])
        name = " ".join(w["text"] for w in name_words).strip()
        name = re.sub(r"[£@†‡#*]", "", name)
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            return None
        for w in weight_words:
            t = w["text"].rstrip("%").replace(",", "")
            try:
                weight = float(t)
            except ValueError:
                continue
            return {"name": name, "weight": weight}
        return None
