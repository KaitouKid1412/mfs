"""DSP Mutual Fund monthly portfolio holdings adapter (Phase 3.C).

DSP's disclosures page is server-rendered HTML that already embeds every
download URL — no SPA / backing API needed. But unlike HDFC (one Excel per
scheme), DSP ships ONE consolidated ZIP per month covering all schemes:

    https://www.dspim.com/media/pages/mandatory-disclosures/
        portfolio-disclosures/<hash>-<ts>/monthend-portfolios_30-april-2026.zip

…and the ZIP contains three workbooks:
    - "DSP Equity ISIN Portfolio as on 30 Apr 2026.xlsx"   (all equity schemes)
    - "DSP Debt ISIN Portfolio as on 30 Apr 2026.xlsx"     (all debt schemes)
    - "Derivatives_Disclosure & Notes Table_April_2026.xlsx"

We only need the EQUITY workbook (the ranked universe lacking holdings is
all equity). It's a Nippon-style multi-sheet consolidated workbook: ONE
sheet per scheme, and the sheet name IS the printed scheme name
('Flexi Cap', 'Large Cap', 'MIDCAP', 'SMALLCAP', …).

Per-sheet layout (validated against DSP Flexi Cap, April 2026):
    row 0: scheme name banner ('DSP Flexi Cap Fund')   in col B
    row 1: 'Portfolio as on April 30, 2026'            in col B
    row 3: header — col A='Sr. No.', col B='Name of Instrument',
           col C='ISIN', col D='Rating/Industry', col E='Quantity',
           col F='Market value (Rs. In lakhs)', col G='% to Net Assets', …
    row 5+: section banners ('EQUITY & EQUITY RELATED' …) + holding rows.
A separate sector-summary table is parked in cols K/L of the same sheet;
its rows carry no ISIN in col C so the generic parser ignores them.

UNIT NOTE: DSP stores '% to Net Assets' as a **fraction** (0.0826 = 8.26%).
We don't special-case this — `parse_sebi_excel` auto-detects fraction vs
percent from the total-weight magnitude and scales fractions up by 100.

DISCOVERY: the ZIP filename encodes the DATA-month-end, but DSP's web team
types it inconsistently across months — observed forms include
``monthend-portfolios_30-april-2026.zip``,
``monthend-portfolio-february-28-2026.zip``,
``monthend-portfolio-january-31-2026.zip``,
``monthend-portfolio-31march2026.zip`` and (older, day-less)
``monthend-portfolio-november-2025.zip``. So we match on the stable parts:
a ``monthend-portfolio`` prefix plus the target month name and 4-digit year
appearing anywhere in the filename. That deliberately excludes the
``dsp-isin-debt-fortnightly-…zip`` and ``half-yearly-…zip`` files on the
same page (different prefix) while tolerating the day/format churn.

SHEET-NAME vs BANNER (printed-name selection): DSP's equity workbook tabs
carry terse internal CODES, not fund names — ``'SMALLCAP'``, ``'MIDCAP'``,
``'TIGER'``, ``'NRNEF'``, ``'DAAF'``, ``'ESF'``. Several fail or MIS-match
the orchestrator's fuzzy matcher against scheme_master:
  - ``'TIGER'`` / ``'NRNEF'`` / ``'DAAF'`` / ``'ESF'`` score far below
    threshold → silently dropped (no holdings for India T.I.G.E.R.,
    Natural Resources & New Energy, Dynamic Asset Allocation, Equity Savings).
  - ``'SMALLCAP'`` (no space) ties at token_set_ratio 100 against BOTH
    ``DSP Small Cap Fund`` and ``DSP Nifty Smallcap 250 Index Fund``; the
    matcher's Levenshtein tie-break wrongly picks the index fund, so the
    ACTIVE Small Cap fund silently gets no holdings.
Every sheet, however, carries the AMC's authoritative full fund name as a
banner in cell B1 (``'DSP Small Cap Fund'``, ``'DSP Equity Savings Fund'``,
``'DSP Natural Resources & New Energy Fund'``, …). So we discover each
scheme by its B1 banner where that yields a better match, and fall back to
the tab name otherwise. Selection rule (`_printed_name_for_sheet`):
  * if the tab name itself fails to match → use the banner;
  * if the tab name is a terse single-token ALL-CAPS code (SMALLCAP, MIDCAP,
    TIGER, …) AND the banner matches at >= the tab's score with a strictly
    tighter Levenshtein ratio to its matched key → use the banner (this is
    what flips SMALLCAP from the index fund to the active fund);
  * otherwise keep the tab name (preserves every already-correct tab,
    including spaced ETF/Index tabs and the standalone MIDCAP tab).
One documented normalization: the standalone Midcap banner ``'DSP Mid Cap
Fund'`` (spaced) token-set-ties 100 with ``'DSP Large & Mid Cap Fund'`` and
loses the tie-break; scheme_master spells it ``'DSP Midcap Fund'`` (no
space), so we collapse exactly that banner before matching. This is anchored
on the full string so it never touches the Large & Mid Cap banner.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path

import openpyxl
from rapidfuzz import fuzz

from mfs import paths
from mfs.db import queries as q
from mfs.ingest.holdings._generic import GenericHoldingsAdapter, parse_sebi_excel
from mfs.ingest.holdings._registry import register_adapter
from mfs.ingest.managers._scheme_match import (
    build_candidate_index,
    canonicalize,
    match_one,
)
from mfs.io.http import fetch_bytes
from mfs.schemas import ParsedHoldingRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = (
    "https://www.dspim.com/mandatory-disclosures/portfolio-disclosures"
)
_DSP_HOST = "https://www.dspim.com"

# Full month names, indexed 1..12. DSP always spells the month out in full
# in the monthend ZIP filename (april / february / march …), even when it
# compresses the rest (e.g. "31march2026").
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

# Any .zip whose filename starts with the monthend-portfolio prefix. We do
# the month/year match in code (not the regex) because the day token and the
# '-'/'_' separators are unstable month-to-month. The prefix anchor excludes
# the debt-fortnightly and half-yearly ZIPs that share this directory.
_ZIP_URL_RE = re.compile(
    r'((?:https://www\.dspim\.com)?/media/pages/mandatory-disclosures/'
    r'portfolio-disclosures/[^"\s]+?/'
    r'(?P<filename>month[_-]?end[_-]portfolio[^"\s/]*?\.zip))',
    re.IGNORECASE,
)

# The equity workbook inside the ZIP. DSP names it "DSP Equity ISIN
# Portfolio as on <DD> <Mon> <YYYY>.xlsx"; we match loosely on the stable
# "Equity ISIN Portfolio" token so a day/month spelling tweak doesn't break us.
_EQUITY_MEMBER_RE = re.compile(r"equity\s+isin\s+portfolio", re.IGNORECASE)


def _normalize_banner(banner: str) -> str:
    """Fix the one DSP banner that collides under the shared fuzzy matcher.

    The active midcap fund's B1 banner is spelled ``'DSP Mid Cap Fund'``
    (spaced), which token-set-ties at 100 with ``'DSP Large & Mid Cap Fund'``
    and loses the Levenshtein tie-break, mis-routing to Large & Mid Cap.
    scheme_master spells the fund ``'DSP Midcap Fund'`` (no space), so we
    collapse exactly that banner. Anchored on the whole (case-insensitive)
    string so the ``'DSP Large & Mid Cap Fund'`` banner is left untouched.
    """
    if banner.strip().lower() == "dsp mid cap fund":
        return "DSP Midcap Fund"
    return banner


def _filename_matches_ym(filename: str, ym: str) -> bool:
    """True if a monthend-portfolio ZIP filename is for data month `ym`.

    Match = the full month name AND the 4-digit year both appear in the
    (lowercased) filename. The day token, if present, is ignored. This
    tolerates 'monthend-portfolios_30-april-2026.zip',
    'monthend-portfolio-february-28-2026.zip', 'monthend-portfolio-
    31march2026.zip', and day-less 'monthend-portfolio-november-2025.zip'.
    """
    y, m = map(int, ym.split("-"))
    month_name = _MONTHS[m - 1]
    fn = filename.lower()
    return month_name in fn and str(y) in fn


def _cache_workbook_path(amc_slug: str, ym: str) -> Path:
    """Path under data/raw/holdings/dsp/<ym>/ for the extracted equity workbook."""
    return paths.holdings_excel_raw(
        amc_slug, ym, f"dsp-equity-isin-portfolio-{ym}.xlsx"
    )


@register_adapter
class DspHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "dsp"
    source_label = "DSP Mutual Fund"

    # Populated by discover_scheme_urls: emitted printed-name → workbook sheet
    # index. Lets parse_excel resolve a printed name back to its source sheet
    # whether the name is a B1 banner or the raw tab name.
    _printed_to_sheet_idx: dict[str, int]

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Locate the monthend ZIP for `ym`, extract+cache the equity
        workbook, and enumerate its per-scheme sheets.

        Each sheet is keyed by the printed name the orchestrator's fuzzy
        matcher resolves best — its B1 banner (the AMC's authoritative full
        fund name) when that beats the terse tab name, else the tab name.
        See `_printed_name_for_sheet` and the module docstring. We stash a
        printed-name → sheet-index map so `parse_excel` can find the source
        sheet whether the key is a banner or a tab name.

        Because DSP ships a single consolidated workbook, every entry maps
        to the same ZIP URL (used only for logging/cache keying — the
        overridden `fetch_excel` returns the already-extracted workbook).
        """
        html = fetch_bytes(_DISCLOSURE_PAGE).decode("utf-8", errors="replace")
        zip_url = self._select_zip_url(html, ym)
        if zip_url is None:
            log.warning("holdings.dsp.no_zip_for_ym", ym=ym)
            return {}

        wb_path = self._ensure_equity_workbook(zip_url, ym)
        if wb_path is None:
            log.warning("holdings.dsp.no_equity_member", ym=ym, zip_url=zip_url)
            return {}

        # scheme_master candidate index for this AMC, used to choose between
        # each sheet's tab name and its B1 banner. Built once per discovery.
        candidates = build_candidate_index(q.scheme_master(), self.amc_slug)

        out: dict[str, str] = {}
        printed_to_sheet_idx: dict[str, int] = {}
        wb = openpyxl.load_workbook(wb_path, read_only=True, data_only=True)
        try:
            for idx, sheet_name in enumerate(wb.sheetnames):
                # Keep only sheets that actually carry a SEBI portfolio table
                # (a parseable ISIN/name/%-to-NAV header). Stray summary or
                # cover sheets, if any, are skipped.
                recs = parse_sebi_excel(
                    wb_path, sheet_name, self.amc_slug, sheet_index=idx,
                )
                if next(iter(recs), None) is None:
                    continue
                banner = wb[sheet_name]["B1"].value
                banner = (
                    re.sub(r"\s+", " ", str(banner)).strip() if banner else ""
                )
                printed = self._printed_name_for_sheet(
                    sheet_name, banner, candidates
                )
                if out.setdefault(printed, zip_url) is zip_url:
                    printed_to_sheet_idx.setdefault(printed, idx)
        finally:
            wb.close()

        # Remember which workbook sheet each emitted printed name came from,
        # scoped to this month, so parse_excel resolves banners → sheets.
        self._printed_to_sheet_idx = printed_to_sheet_idx
        log.info("holdings.dsp.discover", ym=ym, n_schemes=len(out), zip_url=zip_url)
        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _printed_name_for_sheet(
        self, sheet_name: str, banner: str, candidates: dict[str, dict]
    ) -> str:
        """Pick the name (tab vs B1 banner) the matcher resolves best.

        DSP equity tabs are terse internal codes ('SMALLCAP', 'TIGER', …)
        that the shared fuzzy matcher either fails or MIS-routes. The B1
        banner is the AMC's authoritative full fund name. Rule (see module
        docstring): prefer the banner only when it does NOT make matching
        worse —
          * tab name doesn't match at all → use banner;
          * tab name is a terse single-token ALL-CAPS code AND the banner
            matches at >= the tab's score with a strictly tighter Levenshtein
            ratio to its matched key → use banner (flips 'SMALLCAP' off the
            Nifty Smallcap 250 *Index* fund onto the active Small Cap fund);
          * otherwise keep the tab name (preserves every already-correct tab).
        """
        tab = re.sub(r"\s+", " ", sheet_name).strip()
        if not banner:
            return tab
        norm_banner = _normalize_banner(banner)

        def match_info(name: str) -> tuple[str | None, float, float]:
            mr = match_one(name, candidates)
            if mr.matched_scheme_code is None:
                return (None, mr.score, -1.0)
            matched_key = next(
                (k for k, v in candidates.items()
                 if v["scheme_code"] == mr.matched_scheme_code),
                "",
            )
            ratio = fuzz.ratio(canonicalize(name), matched_key)
            return (mr.matched_scheme_code, mr.score, ratio)

        tab_code, tab_score, tab_ratio = match_info(tab)
        ban_code, ban_score, ban_ratio = match_info(norm_banner)

        if tab_code is None:
            # Tab didn't match; the banner is our only shot (may also be None,
            # in which case the orchestrator drops it — that's correct).
            return norm_banner if ban_code is not None else tab

        terse_code = " " not in tab and tab.isupper()
        if (
            terse_code
            and ban_code is not None
            and ban_score >= tab_score
            and ban_ratio > tab_ratio
        ):
            return norm_banner
        return tab

    def _select_zip_url(self, html: str, ym: str) -> str | None:
        """Pick the monthend ZIP whose filename matches data month `ym`."""
        for m in _ZIP_URL_RE.finditer(html):
            url = m.group(1)
            if url.startswith("/"):
                url = _DSP_HOST + url
            if _filename_matches_ym(m.group("filename"), ym):
                return url
        return None

    def _ensure_equity_workbook(self, zip_url: str, ym: str) -> Path | None:
        """Download the monthend ZIP (if not cached), extract the equity
        workbook to the standard holdings cache path, and return it.

        Returns None if the ZIP has no recognizable equity-portfolio member.
        """
        wb_path = _cache_workbook_path(self.amc_slug, ym)
        if wb_path.exists():
            return wb_path
        data = fetch_bytes(zip_url)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next(
                (n for n in zf.namelist() if _EQUITY_MEMBER_RE.search(n)),
                None,
            )
            if member is None:
                return None
            payload = zf.read(member)
        wb_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = wb_path.with_suffix(wb_path.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.rename(wb_path)
        return wb_path

    # ------------------------------------------------------------------
    # Download — short-circuit to the cached extracted workbook
    # ------------------------------------------------------------------

    def fetch_excel(self, url: str, scheme_filename: str, ym: str) -> Path:
        """Return the cached extracted equity workbook for this month.

        DSP's workbook is shared across all schemes in this `ym`, so
        `scheme_filename` is intentionally ignored. `url` is the ZIP URL
        from discovery; we (re)download + extract only if the workbook
        isn't already on disk (idempotent for standalone calls).
        """
        wb_path = _cache_workbook_path(self.amc_slug, ym)
        if wb_path.exists():
            return wb_path
        extracted = self._ensure_equity_workbook(url, ym)
        if extracted is None:
            raise ValueError(
                f"DSP monthend ZIP for {ym} has no equity-portfolio member: {url}"
            )
        return extracted

    # ------------------------------------------------------------------
    # Parse — select this scheme's sheet from the consolidated workbook
    # ------------------------------------------------------------------

    def parse_excel(
        self,
        excel_path: Path,
        scheme_name_printed: str,
        ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        """Parse only the sheet behind `scheme_name_printed`.

        `scheme_name_printed` is the name discover_scheme_urls emitted — a B1
        banner (e.g. 'DSP Small Cap Fund') or a raw tab name. We resolve it to
        the source sheet via the `_printed_to_sheet_idx` map built during
        discovery, falling back to a case-insensitive tab-name lookup if the
        map is absent (e.g. parse_excel called standalone without discovery).

        The shared `parse_sebi_excel` handles header detection, column
        mapping, and fraction→percent scaling; we just point it at the
        right sheet index inside the consolidated workbook.
        """
        idx = getattr(self, "_printed_to_sheet_idx", {}).get(scheme_name_printed)
        if idx is None:
            wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
            try:
                target = re.sub(r"\s+", " ", scheme_name_printed).strip().lower()
                idx = next(
                    (i for i, s in enumerate(wb.sheetnames)
                     if re.sub(r"\s+", " ", s).strip().lower() == target),
                    None,
                )
            finally:
                wb.close()
        if idx is None:
            log.warning(
                "holdings.dsp.sheet_not_found",
                scheme=scheme_name_printed, ym=ym,
            )
            return iter(())
        return parse_sebi_excel(
            excel_path, scheme_name_printed, self.amc_slug, sheet_index=idx,
        )
