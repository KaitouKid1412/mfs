"""Invesco Mutual Fund (India) — factsheet adapter (Phase 3).

Calibrated against the April 2026 combined factsheet at
``data/raw/factsheets/invesco/2026-04.pdf`` (~1.94 MB, 65 pages, 43 scheme
pages followed by lump-sum / SIP performance tables and disclosures).

URL discovery
-------------
The Invesco India site at ``www.invescomutualfund.com`` is a Sitefinity-
hosted SPA. The factsheet tab on
``/literature-and-form?tab=Factsheets`` loads its document table from an
AJAX endpoint discovered by grepping the JS bundle:

    GET https://www.invescomutualfund.com/api/RequestForLiterature?year=YYYY

The response is a single-element JSON list whose first dict has eight
category keys, of which ``Factsheet`` holds the per-month entries with
``DocumentName`` (e.g. ``"Factsheet - April 2026"``), ``DocumentUrl``,
and ``DocumentDate``. ``build_url`` returns the deterministic API URL;
``fetch`` resolves the entry matching the data-month name + year and
downloads the PDF behind that ``DocumentUrl``.

For convenience and reproducibility, the most recent two months also
live at a clean canonical path
``/docs/default-source/factsheet/invesco-mf-factsheet-<monthname>-<YYYY>.pdf``
(occasionally with a leading "---" rather than "-" before the month — Invesco
flips between the two patterns each release). Historical months get
renamed with an opaque UUID suffix once they roll off the top spot, so
those URLs become 404. The API is the only reliable resolver.

Layout findings driving the parser
----------------------------------
* Every scheme page's first non-empty line is ``Invesco India <Scheme>``
  (with a few variants: ``Invesco India - Invesco Pan European Equity
  Fund of Fund`` for the cross-border FoF schemes, ``Invesco India NIFTY
  50 Exchange Traded Fund`` for the ETF). 43 scheme pages span pp 4-46;
  pp 1-3 are market commentary and pp 47-65 are lump-sum / SIP / TER /
  disclosure tables (whose first line does NOT start with "Invesco India").

* PTR is printed in the left column as a single visual row:

      ``Portfolio Turnover Ratio (1 Year) 0.89``

  The value column sits at x0 ~ 170-175. Storage convention is
  fraction — Invesco India ELSS Tax Saver Fund prints ``0.89``
  (i.e. 89%) directly, NOT as a percent. We pass through as-is.
  Pure-debt / FOF / ETF pages legitimately don't print the PTR block.

* pdfplumber's ``extract_text`` heavily column-bleeds the left-column
  blocks with the right-side portfolio table on every scheme page —
  the literal "Portfolio Turnover" line comes back fused with
  unrelated portfolio cells. We therefore extract PTR via word
  positions (``extract_words(use_text_flow=True)``) rather than
  parsing the text-flow output.

* For PTR: anchor on adjacent ``Portfolio`` + ``Turnover`` words in the
  left column (x0 < 200, same y-band), then take the rightmost numeric
  token in that same y-band within the left column. Tolerates the
  ``(1 Year)`` qualifier between the label and the value.

Calibration counts on 2026-04
-----------------------------
* 43 scheme pages detected (pp 4-46)
* ~23 PTR rows (equity + hybrid + arbitrage pages — pure debt /
  FOF / ETF schemes don't print Portfolio Turnover Ratio)
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


# First-line prefix the Invesco India arm uses on every scheme page.
# The few cross-border FoF pages print "Invesco India - Invesco ..." which
# also matches because of the trailing space being optional in the check.
_SCHEME_PREFIX = "Invesco India"

# PTR token: must include a decimal point (Invesco prints 1-4 digit
# integer parts plus 1-3 fractional digits).
_PTR_NUM_RE = re.compile(r"^\d+\.\d{1,3}$")


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme title from a scheme page, or None.

    Invesco's scheme pages all start with a first non-empty line of the
    form ``Invesco India <Name>``. Cover / commentary / lump-sum /
    SIP / TER / disclosure pages start with other strings and yield
    None.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.startswith(_SCHEME_PREFIX):
        return None
    # Collapse runs of whitespace defensively (none observed in 2026-04
    # but cheap insurance).
    first = re.sub(r"\s+", " ", first).strip()
    return first or None


def _find_ptr(words: list[dict]) -> float | None:
    """Return Invesco PTR as a fraction (storage convention) or None.

    Anchors on adjacent ``Portfolio`` + ``Turnover`` words in the left
    column (x0 < 200) within a 3pt y-tolerance, then scans the same
    y-band in the left column for the rightmost ``\\d+\\.\\d+`` token.
    The value column sits at x0 ~ 170-175 across all observed Invesco
    scheme pages.
    """
    anchor: dict | None = None
    n = len(words)
    for i, w in enumerate(words):
        if w["text"] != "Portfolio":
            continue
        if w["x0"] >= 200:
            continue
        # Confirm next adjacent word on the same y-band is "Turnover".
        for j in range(i + 1, min(i + 6, n)):
            v = words[j]
            if v["text"] != "Turnover":
                continue
            if abs(v["top"] - w["top"]) > 3:
                continue
            if not (w["x1"] < v["x0"] < w["x1"] + 30):
                continue
            anchor = w
            break
        if anchor is not None:
            break
    if anchor is None:
        return None
    anchor_y = anchor["top"]
    # Collect all numeric tokens in the same y-band in the left column.
    # The label ends with "(1 Year)" or "(Equity)" etc; the value is the
    # right-most fractional token on the same visual row.
    candidates: list[tuple[float, str]] = []
    for w in words:
        if abs(w["top"] - anchor_y) > 3:
            continue
        if w["x0"] >= 200:
            continue
        if w["x0"] <= anchor["x1"]:
            continue
        if _PTR_NUM_RE.match(w["text"]):
            candidates.append((w["x0"], w["text"]))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p[0], reverse=True)
    try:
        return float(candidates[0][1])
    except ValueError:
        return None


def _resolve_factsheet_url(ym: str) -> str:
    """Resolve the Invesco India factsheet PDF URL for data month ``ym``
    via the literature API.

    Invesco's CMS rewrites historical PDF filenames once they roll off
    the most-recent slot (adding an opaque UUID suffix), so the only
    reliable lookup path is the literature API. Returns the absolute
    ``DocumentUrl`` from the matching entry.

    Raises ``IngestError`` if no entry matches the target month+year,
    consistent with the fail-fast invariant.
    """
    import httpx

    y, m = map(int, ym.split("-"))
    month_name = _MONTH_NAMES[m - 1]
    api_url = (
        f"https://www.invescomutualfund.com/api/RequestForLiterature?year={y:04d}"
    )
    try:
        with httpx.Client(
            timeout=60.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, */*",
            },
            follow_redirects=True,
        ) as c:
            r = c.get(api_url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        raise IngestError(
            f"invesco: literature API failed at {api_url}: {e}"
        ) from e
    if not isinstance(data, list) or not data:
        raise IngestError(
            f"invesco: unexpected literature API shape at {api_url}: "
            f"got {type(data).__name__}"
        )
    entry = data[0]
    docs = entry.get("Factsheet") if isinstance(entry, dict) else None
    if not isinstance(docs, list):
        raise IngestError(
            f"invesco: literature API response has no 'Factsheet' "
            f"category for year {y}."
        )
    needle = f"{month_name} {y}".lower()
    for d in docs:
        if not isinstance(d, dict):
            continue
        name = (d.get("DocumentName") or "").lower()
        if needle in name:
            url = d.get("DocumentUrl") or ""
            if url:
                return url
    raise IngestError(
        f"invesco: no Factsheet entry for {month_name} {y} in the "
        f"literature API ({len(docs)} entries scanned). The CMS may not "
        "have published the issue yet."
    )


@register_adapter
class InvescoAdapter(ManagerAdapter):
    """Invesco Mutual Fund (India) factsheet adapter.

    ``amc_slug = 'invesco'`` matches ``scheme_master.amc_code`` directly,
    so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "invesco"
    source_label = "Invesco Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical Invesco literature API URL for data month
        ``ym='YYYY-MM'``.

        Invesco rotates the public filename of older factsheets (the most
        recent month sits at a clean
        ``/docs/default-source/factsheet/invesco-mf-factsheet-<month>-<yyyy>.pdf``
        path, but historical files are renamed with an opaque UUID suffix
        once a new issue lands). The literature API is the only stable
        resolver, so ``build_url`` returns the deterministic API endpoint
        and ``fetch`` resolves the per-month ``DocumentUrl`` from it.
        """
        y, _ = map(int, ym.split("-"))
        return (
            f"https://www.invescomutualfund.com/api/RequestForLiterature?"
            f"year={y:04d}"
        )

    def fetch(self, ym: str) -> Path:
        """Resolve the data-month factsheet URL via the literature API,
        then download to the canonical cache path.

        Re-uses an existing cached PDF unconditionally — delete the file
        to force a re-fetch.
        """
        from mfs import paths
        from mfs.io.http import download_to

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        pdf_url = _resolve_factsheet_url(ym)
        return download_to(pdf_url, out)

    # ------------------------------------------------------------------
    # PTR / AUM
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
                try:
                    words = page.extract_words(use_text_flow=True)
                except Exception:  # noqa: BLE001
                    continue
                ptr = _find_ptr(words)
                if ptr is None:
                    continue
                if ptr != ptr or ptr <= 0:
                    # NaN / non-positive → drop. Invesco prints a real
                    # fraction or omits the block; zero never appears.
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr,
                    source_amc=self.amc_slug,
                )

    # ------------------------------------------------------------------
    # Holdings — deferred. Invesco's two-column portfolio layout has the
    # same column-bleed issues as the other Phase-3 adapters and the
    # factsheet PDF lacks ISINs. Phase 3.C provides the parallel
    # ISIN-tagged Excel path.
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
