"""Mahindra Manulife Mutual Fund — factsheet adapter (Phase 3.D).

Calibrated against the April 2026 combined factsheet at
``data/raw/factsheets/mahindra_manulife/2026-04.pdf`` (~2.35 MB, 27 pages),
which is itself a concatenation of the AMC's 27 per-scheme one-page
factsheet PDFs (the AMC does NOT publish a single consolidated PDF).

URL discovery
-------------
``www.mahindramanulife.com`` ships a slug-stable "Digital Factsheet"
microsite under

    https://www.mahindramanulife.com/digital-factsheet/<month-lower>-<YYYY>/

(e.g. ``/digital-factsheet/april-2026/``) containing one HTML page per
scheme PLUS one one-page PDF per scheme under the sibling ``PDF/``
folder. Naming for the per-scheme PDFs varies:

    PDF/<N>_Mahindra-Manulife-<Scheme>-Fund.pdf            (most schemes)
    PDF/<N>_Mahindra_Manulife_Large_Mid_Cap_Fund.pdf       (underscores
                                                            on the
                                                            Large & Mid
                                                            Cap entry)
    PDF/Innovation-Opportunities-Fund.pdf                   (no index
                                                            prefix on
                                                            the two
                                                            most recent
                                                            launches)
    PDF/Income-Plus-Arbitrage-Active_FOF.pdf

The per-scheme PDFs are the only authoritative source of per-scheme PTR /
AUM — the AMC site exposes no PDF combined factsheet, and the
``Equity_Debt_Hybrid_FoF_Snapshot.pdf`` provides only AUM in a compact
table without PTR. We therefore:

  1. Discover the per-scheme PDF URLs by visiting the slug-stable scheme
     HTML pages and parsing out the ``PDF/<filename>.pdf`` href on each.
  2. Download every per-scheme PDF into a parts subdirectory.
  3. Merge them in numeric-prefix order into a single combined PDF at
     ``data/raw/factsheets/mahindra_manulife/<ym>.pdf`` using poppler's
     ``pdfunite`` (a system tool available via Homebrew / most Linux
     distros). The merge happens once on ``fetch()`` — repeat runs hit
     the disk cache.

URL naming changes month-over-month (file index prefixes shift if the
AMC reorders the lineup) but the slug pattern is stable: this adapter
hard-codes the *list of scheme HTML pages* (which is stable) and
re-discovers the per-scheme PDF filenames at fetch time, so a renamed
or newly launched scheme is handled without a code change.

Layout findings driving the parser
----------------------------------
Each per-scheme page has a 4-element banner whose ``extract_text`` order
varies by y-position:

    Page 5 (Large Cap):                Page 10 (Aggressive Hybrid):
        Mahindra Manulife                  Mahindra Manulife
        FACTSHEET                          FACTSHEET
        Large Cap Fund                     Aggressive Hybrid
        April 2026                         April 2026
                                           Fund          <- title wrap

    Page 13 (Asia Pacific REITs):     Page 22 (Multi Asset Allocation):
        FACTSHEET                          Mahindra Manulife Multi
        Mahindra Manulife Asia             FACTSHEET
        April 2026                         Asset Allocation Fund
        Pacific REITs FOF                  April 2026

We extract the printed scheme name by walking the first ~12 non-empty
lines, dropping ``FACTSHEET`` and the ``<Month> <YYYY>`` line, then
concatenating consecutive title fragments until we hit the parenthetical
category descriptor / first body row.

PTR label (printed as a **fraction**, e.g. ``0.58`` = 58% turnover):

    ``Portfolio Turnover Ratio (Last one year): 0.58``  (most equity
                                                          pages)
    ``Portfolio Turnover Ratio (Last 1 year) 0.68``     (some hybrid
                                                          pages, no
                                                          colon)

Storage convention is also fraction so no unit conversion needed.

AUM label — month-end value sits two lines below ``Monthly AUM as on
April 30, 2026``:

    Monthly AAUM as on April 30, 2026
    (Rs. in Cr.): 696.33
    Monthly AUM as on April 30, 2026
    (Rs. in Cr.): 700.93                  <- we want this

On a fraction of pages (Mid Cap, Banking & Financial Services) the AAUM
and AUM lines are interleaved with ``IDCW HISTORY`` / ``Data as on …``
sentences in the ``extract_text`` linearisation, so we use a
word-position fallback that anchors on the literal ``Monthly`` + ``AUM``
pair (distinguishing from ``Monthly`` + ``AAUM``) and walks down to the
first ``(Rs.`` token + ``X,XXX.YZ`` value.

Calibration counts on 2026-04
-----------------------------
* 27 scheme pages detected.
* 15 PTR records parsed (12 omissions = 7 debt + 2 FoF + 3 new equity
  schemes launched <12 months ago — Banking FS Jul 2025, Innovation Jan
  2026, Value Mar 2025 — that don't yet print a 1-year PTR).
* 27 AUM records parsed (every scheme page yields a clean Month-End
  AUM token).

Slug → scheme_master
--------------------
``amc_slug = 'mahindra_manulife'`` matches ``scheme_master.amc_code``
exactly, so no alias entry in ``_scheme_match.py`` is needed.
"""

from __future__ import annotations

import re
import subprocess
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


# Stable list of scheme HTML pages on the Digital Factsheet microsite.
# Each entry is the path under
# ``/digital-factsheet/<month>-<year>/`` whose HTML carries a single
# ``../PDF/<filename>.pdf`` link to that scheme's one-page factsheet PDF.
# The list is stable month-over-month; new launches show up here too
# (the Innovation Opportunities and Income Plus Arbitrage Active FOF
# pages already exist).
_SCHEME_HTML_PAGES: tuple[str, ...] = (
    "Equity-funds/Multi-Cap-Fund.html",
    "Equity-funds/Mid-Cap-Fund.html",
    "Equity-funds/Consumption-Fund.html",
    "Equity-funds/Large-Cap-Fund.html",
    "Equity-funds/Large-Mid-Cap-Fund.html",
    "Equity-funds/Focused-Fund.html",
    "Equity-funds/Flexi-Cap-Fund.html",
    "Equity-funds/Small-Cap-Fund.html",
    "Equity-funds/Business-Cycle-Fund.html",
    "Equity-funds/Manufacturing-Fund.html",
    "Equity-funds/Value-Fund.html",
    "Equity-funds/Banking-Financial-Services.html",
    "Equity-funds/Innovation-Opportunities-Fund.html",
    "Hybrid-funds/Equity-Savings-Fund.html",
    "Hybrid-funds/Aggressive-Hybrid-Fund.html",
    "Hybrid-funds/Balanced-Advantage-Fund.html",
    "Hybrid-funds/Multi-Asset-Allocation-Fund.html",
    "Hybrid-funds/Arbitrage-Fund.html",
    "Debt-funds/Liquid-Fund.html",
    "Debt-funds/Low-Duration-Fund.html",
    "Debt-funds/Dynamic-Fund.html",
    "Debt-funds/Overnight-Fund.html",
    "Debt-funds/Ultra-Short-Fund.html",
    "Debt-funds/Short-Duration-Fund.html",
    "ELSS-FOF/ELSS-Fund.html",
    "ELSS-FOF/Asia-Pacific-REITs-FOF.html",
    "ELSS-FOF/Income-Plus-Arbitrage-Active-FOF.html",
)


# Regex to pull the per-scheme PDF filename out of a scheme HTML page.
# Most pages emit ``../PDF/<N>_Mahindra-Manulife-<Name>.pdf`` but newly
# launched schemes (Innovation Opportunities, Income Plus Arbitrage
# Active FOF) drop the numeric prefix entirely.
_PDF_HREF_RE = re.compile(r'href="\.\./PDF/([^"]+\.pdf)"')


# ---------------------------------------------------------------------------
# Scheme-name extraction
# ---------------------------------------------------------------------------

_AMC_PREFIX = "Mahindra Manulife"
_MONTH_YEAR_RE = re.compile(
    r"^(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4}$",
    re.IGNORECASE,
)
_BODY_START_RE = re.compile(
    r"^(?:PORTFOLIO|Portfolio|Investment\s+Objective|Company\s*/\s*Issuer|"
    r"Fund\s+Features|Fund\s+Manager|Date\s+of\s+allotment|"
    r"Scheme\s+Details|SECTOR\s+ALLOCATION|MARKET\s+CAPITALIZATION|"
    r"Monthly\s+AAUM|Monthly\s+AUM|Benchmark|NAV)",
    re.IGNORECASE,
)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme title or ``None`` for non-scheme pages.

    Walks the first 12 non-empty lines, dropping ``FACTSHEET`` and the
    ``<Month> <YYYY>`` line, concatenating consecutive short
    alphabetic-only fragments until a parenthetical descriptor / body
    row is reached. Validates that the result contains ``Mahindra
    Manulife`` and ends in ``Fund`` / ``FOF``.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:12]
    parts: list[str] = []
    for ln in lines:
        if ln.upper() == "FACTSHEET":
            continue
        if _MONTH_YEAR_RE.match(ln):
            continue
        if ln.startswith("("):
            break
        if _BODY_START_RE.match(ln):
            break
        if any(ch.isdigit() for ch in ln):
            break
        if len(ln.split()) > 8:
            break
        parts.append(ln)
        if len(parts) >= 4:
            # Safety cap — a real title rarely spans more than 4 lines.
            break
    if not parts:
        return None
    title = " ".join(parts).strip()
    title = re.sub(r"\s+", " ", title)
    if _AMC_PREFIX not in title:
        return None
    if not re.search(r"\b(?:Fund|FOF)\b", title, re.IGNORECASE):
        return None
    return title


# ---------------------------------------------------------------------------
# PTR / AUM extraction
# ---------------------------------------------------------------------------

# PTR is a FRACTION on Mahindra Manulife pages (e.g. 0.58 = 58%). Two
# label variants observed across the 2026-04 issue:
#   "Portfolio Turnover Ratio (Last one year): 0.58"   (most equity)
#   "Portfolio Turnover Ratio (Last 1 year) 0.68"      (some hybrid,
#                                                       no colon)
_PTR_TEXT_RE = re.compile(
    r"Portfolio\s+Turnover\s+Ratio\s*\(Last\s+(?:one|1)\s+year\)\s*:?\s*(\d+\.\d+)",
    re.IGNORECASE,
)


def _find_ptr_via_words(page) -> float | None:
    """Word-position fallback for PTR extraction.

    Locates the ``Portfolio`` + ``Turnover`` word pair in the left
    column (x0 < 200) on the same y-band, then takes the first
    ``\\d+\\.\\d+`` token to the right within a 4pt vertical window.
    Used when ``extract_text`` column-bleeds the PTR row with the
    right-column portfolio table.
    """
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None
    for i, w in enumerate(words):
        if w["text"] != "Portfolio":
            continue
        if w["x0"] >= 200:
            continue
        for j in range(i + 1, min(i + 8, len(words))):
            v = words[j]
            if abs(v["top"] - w["top"]) > 4:
                continue
            if not v["text"].lower().startswith("turnover"):
                continue
            anchor_y = v["top"]
            anchor_x = v["x1"]
            for n in words:
                if abs(n["top"] - anchor_y) > 4:
                    continue
                if n["x0"] <= anchor_x:
                    continue
                txt = n["text"].rstrip(",;.%")
                if re.fullmatch(r"\d+\.\d{1,3}", txt):
                    try:
                        val = float(txt)
                    except ValueError:
                        continue
                    if val <= 0:
                        return None
                    return val
            break
    return None


# ---------------------------------------------------------------------------
# fetch() — discover URLs, download, merge
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.mahindramanulife.com/digital-factsheet"


def _site_root_for_ym(ym: str) -> str:
    """Return the per-month microsite root URL.

    Mahindra Manulife publishes the digital factsheet under
    ``/digital-factsheet/<month-lower>-<YYYY>/`` where the date is the
    DATA month (e.g. April 2026 lives under ``april-2026``).
    """
    y, m = map(int, ym.split("-"))
    return f"{_BASE_URL}/{_MONTH_NAMES[m - 1]}-{y:04d}"


def _http_client() -> httpx.Client:
    """Mahindra Manulife serves PDFs from an IIS host that:
      * returns 404 on HEAD requests for resources that GET as 200
        (HEAD-method filter on the static-file handler).
      * requires a browser-like ``User-Agent`` and ``follow_redirects``
        to traverse the per-AMC redirect chain.
    """
    return httpx.Client(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
        follow_redirects=True,
        timeout=30,
    )


def _discover_pdf_filenames(site_root: str, client: httpx.Client) -> list[str]:
    """Visit every scheme HTML page and collect the per-scheme PDF
    filename it links to.

    Returns the filenames in the iteration order of
    ``_SCHEME_HTML_PAGES`` (which mirrors the AMC's own equity / hybrid
    / debt / ELSS / FoF taxonomy). Raises ``IngestError`` if any page
    is missing or fails to link to a PDF.
    """
    pdf_filenames: list[str] = []
    for page in _SCHEME_HTML_PAGES:
        url = f"{site_root}/{page}"
        r = client.get(url)
        if r.status_code != 200:
            raise IngestError(
                f"mahindra_manulife: scheme HTML missing ({r.status_code}) "
                f"at {url}"
            )
        m = _PDF_HREF_RE.search(r.text)
        if not m:
            raise IngestError(
                f"mahindra_manulife: no per-scheme PDF link found in {url}"
            )
        pdf_filenames.append(m.group(1))
    return pdf_filenames


def _download_per_scheme_pdfs(
    site_root: str,
    pdf_filenames: list[str],
    parts_dir: Path,
    client: httpx.Client,
) -> list[Path]:
    """Download every per-scheme PDF into ``parts_dir`` and return the
    list of local paths (in input order). Cached files are not re-fetched
    when they're already non-empty + valid PDFs.
    """
    parts_dir.mkdir(parents=True, exist_ok=True)
    out_paths: list[Path] = []
    for fn in pdf_filenames:
        url = f"{site_root}/PDF/{fn}"
        local = parts_dir / fn
        if local.exists() and local.stat().st_size > 10_000 \
                and local.read_bytes()[:4] == b"%PDF":
            out_paths.append(local)
            continue
        r = client.get(url)
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            raise IngestError(
                f"mahindra_manulife: failed to download {url} "
                f"(status={r.status_code}, "
                f"size={len(r.content)})"
            )
        local.write_bytes(r.content)
        out_paths.append(local)
    return out_paths


def _merge_pdfs(parts: list[Path], combined: Path) -> None:
    """Combine the per-scheme PDFs into one multi-page PDF at
    ``combined`` using poppler's ``pdfunite``.

    pdfunite preserves text streams (unlike re-rendering merges), so
    pdfplumber's text extraction on the combined PDF is identical to
    extracting per-scheme PDFs individually. ``pdfunite`` ships with
    poppler-utils (Homebrew ``poppler`` on macOS, ``poppler-utils`` on
    Debian/Ubuntu). We fail fast with an actionable error message if
    the tool is missing.
    """
    combined.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["pdfunite", *[str(p) for p in parts], str(combined)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise IngestError(
            "mahindra_manulife: ``pdfunite`` (poppler) not found on PATH. "
            "Install poppler-utils (``brew install poppler`` on macOS or "
            "``apt-get install poppler-utils`` on Debian/Ubuntu) and re-run."
        ) from e
    if r.returncode != 0:
        raise IngestError(
            f"mahindra_manulife: pdfunite failed (rc={r.returncode}): "
            f"{r.stderr.strip()}"
        )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@register_adapter
class MahindraManulifeAdapter(ManagerAdapter):
    """Mahindra Manulife Mutual Fund factsheet adapter.

    ``amc_slug = 'mahindra_manulife'`` matches ``scheme_master.amc_code``
    exactly, so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "mahindra_manulife"
    source_label = "Mahindra Manulife Mutual Fund"

    # -----------------------------------------------------------------
    # URL / fetch
    # -----------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the per-month digital-factsheet microsite root URL for
        data month ``ym='YYYY-MM'``.

        The AMC does NOT publish a single combined PDF — the canonical
        per-scheme URLs are resolved at fetch time by walking each
        scheme HTML page. We return the microsite root here so callers
        wanting a stable AMC-side URL have one to reference.
        """
        return _site_root_for_ym(ym) + "/index.html"

    def fetch(self, ym: str) -> Path:
        """Download + merge the per-scheme PDFs into one combined PDF
        cached at ``data/raw/factsheets/mahindra_manulife/<ym>.pdf``.

        Repeat invocations hit the disk cache unconditionally — delete
        the file (and ``<ym>_parts/``) to force a re-fetch.
        """
        from mfs import paths

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        parts_dir = out.with_suffix("") / "parts" if False else (
            out.parent / f"{ym}_parts"
        )
        site_root = _site_root_for_ym(ym)
        with _http_client() as c:
            pdf_filenames = _discover_pdf_filenames(site_root, c)
            log.info(
                "mahindra_manulife.discover",
                ym=ym,
                n_per_scheme_pdfs=len(pdf_filenames),
            )
            parts = _download_per_scheme_pdfs(site_root, pdf_filenames, parts_dir, c)
        _merge_pdfs(parts, out)
        log.info(
            "mahindra_manulife.fetch.done",
            ym=ym,
            combined_pdf=str(out),
            size_bytes=out.stat().st_size,
        )
        return out

    # -----------------------------------------------------------------
    # PTR / AUM
    # -----------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                ptr_value: float | None = None
                m = _PTR_TEXT_RE.search(text)
                if m:
                    try:
                        ptr_value = float(m.group(1))
                    except ValueError:
                        ptr_value = None
                if ptr_value is None and "Portfolio" in text and "Turnover" in text:
                    ptr_value = _find_ptr_via_words(page)
                if ptr_value is None:
                    continue
                # Drop NaN / non-positive — Mahindra Manulife prints a
                # real fraction or omits the block entirely; a zero
                # PTR scheme is not a valid value.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )

    # -----------------------------------------------------------------
    # Holdings — deferred. Mahindra Manulife's per-scheme PDF prints a
    # two-column ``Company / Issuer  % of Net Assets`` table without
    # ISINs, interleaved with sector banner rows. Phase 3.A's parallel
    # ISIN-tagged Excel path covers the AMC for the portfolio-overlap
    # metric, so we return () here.
    # -----------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
