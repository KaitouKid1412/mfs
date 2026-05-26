r"""Baroda BNP Paribas Mutual Fund — factsheet adapter (Phase 3).

Calibrated against the April 2026 combined factsheet PDF
(``data/raw/factsheets/baroda_bnp/2026-04.pdf``, ~2.79 MB, 75 pages, 49
scheme pages plus cover / market commentary / SIP performance / IDCW
history / disclosures at the back).

URL discovery
-------------
``www.barodabnpparibasmf.in`` is a server-rendered PHP CMS. The monthly
factsheet permalink is

    https://www.barodabnpparibasmf.in/downloads/monthly-factsheet

and the page surfaces direct PDF links for the latest issue under
``/assets/download_documents/BBNPP_MF_Fund_Facts_<MonthName>_<YYYY>-Final_<id>.pdf``.
The trailing ``<id>`` is a per-upload integer suffix that is NOT
deterministic month-over-month, so we cannot construct the absolute PDF
URL from ``ym`` alone. ``build_url(ym)`` therefore returns the permalink
page URL (deterministic), and ``fetch(ym)`` resolves it to the actual
``*.pdf`` by scanning the HTML for the matching month's link
(mirroring the Bandhan adapter pattern).

Layout findings driving the parser
----------------------------------
* Every scheme page renders the title in the top-left of the page in a
  larger font split across 1-3 visual lines, e.g.

      ``Baroda BNP Paribas``
      ``Large Cap Fund``

  or for longer names like Banking & Financial Services:

      ``Baroda BNP Paribas``
      ``Banking and Financial``
      ``Services Fund``

  Word positions: y in [22, 80], x1 <= 215 (left column). The right half
  of the page in that band carries the riskometer caption ("This product
  is suitable for investors..."), so the x-cap is essential. We assemble
  by y-row grouping and validate that the cleaned string starts with
  ``Baroda BNP Paribas`` (a handful of late-cover pages have
  ``Baroda BNP Paribas Mutual Fund`` in a footer banner; those are
  filtered out by the FUND/ETF suffix check).

* PTR is printed as ``Portfolio Turnover Ratio : 0.69`` in the left
  column at x0 ~ 30 (label) / ~ 126 (value). pdfplumber's
  ``extract_text`` column-bleeds the line on most equity / hybrid pages
  because the right-side holdings table overlaps the same y-band, so
  the plain text often shows ``Turnover`` without the value attached.
  We use word-position extraction throughout: find the ``Portfolio`` +
  ``Turnover`` + ``Ratio`` word triple in the left column (x0 < 110)
  on the same y-band, then take the first numeric token (``\d+(?:\.\d+)?``)
  to the right of the label in the same y-band.

  PTR is already a fraction in Baroda BNP factsheets (Large Cap Fund =
  0.69, i.e. 69%) — no percent-to-fraction conversion. Arbitrage Fund
  prints higher values (12.80 ~ 1280%) which is normal for arbitrage
  schemes that turn over the entire book multiple times a month.

* AUM is printed in the left column at y ~ 304 on most pages as

      ``AUM## As on April 30, 2026 : v2,578.67 Crores``

  with a sibling ``Monthly AAUM## ...`` row immediately above. The
  ``v`` glyph is pdfplumber's rendering of the INR rupee symbol that
  fuses to the value (e.g. ``v2,578.67``). On many pages this line
  column-bleeds with the right-side holdings table — the underlying
  word positions remain clean. We anchor on the ``AUM##`` token in the
  left column (x0 < 100) and take the first ``v?<number>`` token on the
  same y-band to the right, stripping the leading ``v``/``₹``/`` ` ``.

  The ``Monthly AAUM##`` line is deliberately ignored — Stage 2 stores
  point-in-time month-end AUM, not the monthly average. The
  ``Monthly`` prefix on the AAUM token disambiguates: we require an
  exact match on ``AUM##`` (no leading ``Monthly``).

  Values are in INR Crore — no unit conversion.

Calibration counts on 2026-04
-----------------------------
* 49 scheme pages detected (pp 7-55, with pp 24/27/45 as
  allocation-chart continuation pages that legitimately yield nothing).
* PTR rows extracted: ~30 (equity + hybrid + arbitrage + index/ETF;
  pure debt schemes don't print the Portfolio Turnover line).
* AUM rows extracted: ~45 (every scheme page that prints
  ``AUM## As on ...``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

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


# Scheme-name acceptance: cleaned title must start with this prefix.
_SCHEME_PREFIX = "Baroda BNP Paribas"

# Suffix anchors: the canonical scheme name ends at one of these tokens.
# We try the **longer** "Fund of Fund(s)" variant FIRST because Python
# regex alternation with a non-greedy ``.*?`` picks the shortest valid
# suffix, which would otherwise truncate
# ``Baroda BNP Paribas Gold ETF Fund of Fund`` to ``Baroda BNP Paribas
# Gold ETF`` and conflate it with the standalone Gold ETF scheme.
_NAME_FOF_RE = re.compile(
    r"^(Baroda\s+BNP\s+Paribas\s+.*?Fund\s+of\s+Funds?)\b",
    re.IGNORECASE,
)
_NAME_SUFFIX_RE = re.compile(
    r"^(Baroda\s+BNP\s+Paribas\s+.*?(?:ETF|FOF|Fund))\b",
    re.IGNORECASE,
)

# A numeric token that may be prefixed by the rupee glyph (``v``, ``₹``,
# ```` ` ``) and may include thousands separators.
_NUMERIC_RE = re.compile(r"^[v`₹]?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)$")


def _scheme_name_from_page(page) -> str | None:
    """Return the canonical printed scheme title from a page, or None.

    The title sits at the top-left of every scheme page within the
    band y in [22, 80] and x1 <= 215. We group word tokens into rows
    by y (4pt tolerance), join row tokens by ascending x, then join
    rows by space. The composite string is then trimmed to the first
    ``Fund`` / ``Fund of Fund(s)`` / ``FOF`` / ``ETF`` boundary so the
    trailing ``(An open ended ...)`` descriptor is dropped.
    """
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None
    band = [w for w in words if 22 <= w["top"] <= 80 and w["x1"] <= 215]
    if not band:
        return None
    band.sort(key=lambda w: (round(w["top"]), w["x0"]))
    rows: list[list[dict]] = []
    for w in band:
        if not rows or abs(w["top"] - rows[-1][-1]["top"]) > 6:
            rows.append([w])
        else:
            rows[-1].append(w)
    parts: list[str] = []
    for row in rows:
        row.sort(key=lambda w: w["x0"])
        parts.append(" ".join(w["text"] for w in row))
    composite = " ".join(parts).strip()
    if not composite:
        return None
    if not composite.startswith(_SCHEME_PREFIX):
        return None
    m = _NAME_FOF_RE.match(composite) or _NAME_SUFFIX_RE.match(composite)
    if not m:
        return None
    name = m.group(1).strip()
    # Reject the "Baroda BNP Paribas Mutual Fund" footer banner that
    # appears on a couple of non-scheme pages.
    if re.fullmatch(r"Baroda\s+BNP\s+Paribas\s+Mutual\s+Fund", name, re.IGNORECASE):
        return None
    return name


def _find_value_via_words(words: list[dict], anchors: tuple[str, ...]) -> float | None:
    """Locate the value next to a left-column label by word positions.

    Generic helper: find the *anchors* tuple (e.g. ``("Portfolio",
    "Turnover", "Ratio")`` or ``("AUM##",)``) appearing in adjacent
    left-column words on the same y-band, then return the first
    rupee-glyph-stripped numeric token to the right of the last anchor
    word on the same y-band.

    Returns None if no matching anchor / value pair is found.
    """
    n = len(words)
    if n == 0:
        return None
    needle = list(anchors)
    k = len(needle)
    for i in range(n - k + 1):
        cand = words[i : i + k]
        # All anchor words present in order, same y-band, left column.
        if [w["text"] for w in cand] != needle:
            continue
        if cand[0]["x0"] >= 110:
            continue
        ys = [w["top"] for w in cand]
        if max(ys) - min(ys) > 3:
            continue
        last = cand[-1]
        y_anchor = last["top"]
        x_min = last["x1"]
        # Find the first numeric token to the right of the last anchor
        # word on the same y-band (4pt tolerance).
        candidates: list[tuple[float, float]] = []
        for v in words:
            if abs(v["top"] - y_anchor) > 4:
                continue
            if v["x0"] <= x_min:
                continue
            m = _NUMERIC_RE.match(v["text"])
            if not m:
                continue
            try:
                candidates.append((v["x0"], float(m.group(1).replace(",", ""))))
            except ValueError:
                continue
        if not candidates:
            continue
        candidates.sort()
        return candidates[0][1]
    return None


def _parse_ptr_from_page(page) -> float | None:
    """Return the printed Portfolio Turnover Ratio as a fraction, or None."""
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:  # noqa: BLE001
        return None
    return _find_value_via_words(words, ("Portfolio", "Turnover", "Ratio"))


@register_adapter
class BarodaBnpAdapter(ManagerAdapter):
    """Baroda BNP Paribas Mutual Fund factsheet adapter.

    ``amc_slug = 'baroda_bnp'`` does NOT match ``scheme_master.amc_code``
    (which is ``baroda_bnp_paribas``). The alias entry in
    ``_scheme_match._ADAPTER_SLUG_TO_SCHEME_MASTER_AMC_CODE`` bridges that
    gap — without it the fuzzy-match would silently match zero schemes.
    """

    amc_slug = "baroda_bnp"
    source_label = "Baroda BNP Paribas Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical CMS permalink for data month ym='YYYY-MM'.

        The permalink page always surfaces the latest issue (and a
        load-more dropdown for archived months); ``fetch(ym)`` scans the
        HTML for the matching ``BBNPP_MF_Fund_Facts_<MonthName>_<YYYY>``
        PDF link rather than guessing the per-upload numeric suffix.
        """
        return "https://www.barodabnpparibasmf.in/downloads/monthly-factsheet"

    def fetch(self, ym: str) -> Path:
        """Download the data-month combined factsheet to the cache path.

        The factsheet permalink lists multiple months but only surfaces
        one with a direct ``BBNPP_MF_Fund_Facts_<MonthName>_<YYYY>-Final_<id>.pdf``
        link in the static HTML; older months are loaded via an
        AJAX endpoint that requires a CSRF token. For Phase-3 we only
        need the most recent month (the default ``_default_data_month``
        in ``_run.py``), which is what the permalink already shows.

        Re-uses an existing cached PDF unconditionally — delete the file
        to force a re-fetch.
        """
        from mfs import paths
        from mfs.io.http import download_to, fetch_bytes

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out

        y, m = map(int, ym.split("-"))
        month_name = _MONTH_NAMES[m - 1]
        permalink = self.build_url(ym)
        try:
            html_bytes = fetch_bytes(permalink)
        except Exception as e:  # noqa: BLE001
            raise IngestError(
                f"baroda_bnp: failed to fetch permalink {permalink}: {e}"
            ) from e
        html = html_bytes.decode("utf-8", errors="replace")
        pdf_urls = re.findall(r"https?://[^\"'\s<>\\]+?\.pdf", html)
        # Filter for the BBNPP factsheet matching the target month/year.
        needle_month = month_name.lower()
        needle_year = str(y)
        candidates = [
            u
            for u in pdf_urls
            if "bbnpp" in u.lower()
            and "fund_facts" in u.lower()
            and needle_month in u.lower()
            and needle_year in u.lower()
        ]
        if not candidates:
            raise IngestError(
                f"baroda_bnp: no BBNPP fund-facts PDF for {month_name} {y} "
                f"found on {permalink} (scanned {len(pdf_urls)} pdf links). "
                "The CMS only surfaces the latest issue; older months would "
                "need the AJAX endpoint."
            )
        return download_to(candidates[0], out)

    # -----------------------------------------------------------------
    # PTR
    # -----------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        import logging as _logging
        _logging.getLogger("pdfminer").setLevel(_logging.ERROR)
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                scheme = _scheme_name_from_page(page)
                if not scheme:
                    continue
                ptr = _parse_ptr_from_page(page)
                if ptr is None:
                    continue
                # Fail-fast: NaN / non-positive → drop.
                if ptr != ptr or ptr <= 0:
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr,
                    source_amc=self.amc_slug,
                )

    # -----------------------------------------------------------------
    # Holdings — deferred. The factsheet's two-column portfolio layout
    # has the same column-bleed issues as HDFC/Kotak/Bandhan and no
    # ISIN column. Phase 3.C provides the parallel Excel-based
    # ISIN-tagged path.
    # -----------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
