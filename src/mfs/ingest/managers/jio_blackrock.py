"""JioBlackRock Mutual Fund — factsheet PTR adapter.

JioBlackRock is a 50:50 JV between Jio Financial Services and BlackRock
(SEBI final approval May 2025; first NFOs from Aug 2025). It publishes a
single combined monthly "Factsheet" PDF covering all live schemes.

Layout findings (calibrated against the combined factsheets that are
publicly retrievable today — 31-Aug-2025 and 30-Sep-2025 editions, both
26 pages; the April-2026 edition follows the SAME layout family)
----------------------------------------------------------------------
* Each scheme has its own page (sometimes two). The first non-empty line
  of a scheme page is the as-on banner ``(Data as on 30th September, 2025)``;
  the scheme name is the VERY NEXT non-empty line, e.g.
  ``JioBlackRock Nifty 50 Index Fund``. We use that pairing as the
  scheme-name anchor — it is unambiguous and present on every scheme page,
  unlike the page footer which prints the same name but is easier to
  column-bleed.

* PTR is printed in a ``QUANTITATIVE DATA`` block as a **percent**:
  ``Portfolio Turnover 1.34%``. Storage is a fraction (1.34% -> 0.0134),
  so we DIVIDE BY 100 after extraction. (This is the most common adapter
  bug — JioBlackRock prints percent, not a bare fraction.)

* In the early (2025) editions only the passive index funds carried a PTR;
  the active equity funds (Flexi Cap, Large Cap, Sector Rotation) had not
  launched / were too young to report turnover. By the April-2026 data
  month those funds should print ``Portfolio Turnover X%`` in the same
  block and will be picked up automatically — no per-scheme special-casing.

* Other risk metrics in the block: ``Tracking Error`` (passive funds),
  and for active funds the standard ``Standard Deviation / Beta / Sharpe``
  set. We only extract ``Portfolio Turnover``.

URL / fetch
-----------
JioBlackRock does NOT expose a stable, date-derivable factsheet URL. The
factsheet listing page
(``/statutory-disclosure/fund-documents/factsheet``) is a client-side
Next.js component that fetches the document index from an
authentication-gated Strapi API
(``service.jioblackrockamc.com/v1/jiobr/api/disclosure-l3s``, bearer-token
required, returns empty without it). The actual PDFs are served from
``jioinvest.cdn.jio.com`` under OPAQUE random-token filenames
(e.g. ``jioblackro-mgrls723-5057de.pdf`` for the Sep-2025 edition) that
cannot be reconstructed from the data month.

Consequently ``build_url`` cannot synthesise the URL. We override
``fetch`` to:
  1. Re-use an already-cached ``<slug>/<ym>.pdf`` if a human/operator has
     dropped the resolved PDF there (the normal happy path once the URL is
     known), and
  2. Otherwise raise ``IngestError`` with a precise message rather than
     guessing — fail-fast per the pipeline invariants.

``build_url`` is still implemented (it returns the known CDN host + the
documented filename pattern) so the contract is satisfied and the error
message can point at the right place, but it is intentionally NOT used to
auto-download — the token is not derivable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pdfplumber

from mfs.errors import IngestError
from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.schemas import ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _publish_ym(data_ym: str) -> str:
    """data month YYYY-MM -> publish month YYYY-MM (data + 1, year rollover).

    JioBlackRock publishes the "April 2026" combined factsheet (data as on
    30-Apr-2026) in May 2026, mirroring every other AMC. Encoded as a
    pattern so a future month's publish-shift is handled automatically.
    """
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{m + 1:02d}"


# ---------------------------------------------------------------------------
# Scheme-name detection
# ---------------------------------------------------------------------------

# Banner that opens every scheme page: "(Data as on 30th September, 2025)".
_DATA_ASON_RE = re.compile(r"^\(Data as on .+?\)$", re.IGNORECASE)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name (line after the ``(Data as on ...)``
    banner) or ``None`` for non-scheme pages (cover, TOC, market commentary,
    glossary, disclosures).
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if _DATA_ASON_RE.match(ln) and i + 1 < len(lines):
            name = re.sub(r"\s+", " ", lines[i + 1]).strip()
            # The name line must look like a JioBlackRock scheme banner, not
            # a stray "Category: ..." or numeric row.
            if name.lower().startswith("jioblackrock"):
                return name
            return None
    return None


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# JioBlackRock prints PTR as a PERCENT: "Portfolio Turnover 1.34%". Storage
# is a fraction (1.34% -> 0.0134), so we divide by 100 after extraction.
_PTR_TEXT_RE = re.compile(
    r"Portfolio\s+Turnover\s+(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)


@register_adapter
class JioBlackRockAdapter(ManagerAdapter):
    """JioBlackRock Mutual Fund factsheet adapter (PTR only)."""

    amc_slug = "jio_blackrock"
    source_label = "JioBlackRock Mutual Fund"

    # -------------------------------------------------------------------
    # URL / fetch
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Return the CDN host + documented filename pattern for data month ym.

        NOTE: JioBlackRock factsheet PDFs use OPAQUE random-token filenames
        (``jioblackro-<token>-<token>.pdf``) served from the Jio CDN and
        indexed only via an auth-gated Strapi document API. The token is not
        derivable from ``ym``, so this URL is illustrative — ``fetch`` does
        NOT auto-download from it. It exists to satisfy the adapter contract
        and to point operators at the right host/listing page.
        """
        publish = _publish_ym(ym)
        _, m = ym.split("-")
        month_name = _MONTH_NAMES[int(m) - 1]
        log.info(
            "jio_blackrock.build_url.note",
            data_ym=ym,
            publish_ym=publish,
            month=month_name,
            listing_page=(
                "https://www.jioblackrockamc.com/"
                "statutory-disclosure/fund-documents/factsheet"
            ),
        )
        # Pattern only — the <token> segments are server-assigned and opaque.
        return "https://jioinvest.cdn.jio.com/jioblackro-<token>.pdf"

    def fetch(self, ym: str) -> Path:
        """Return the cached factsheet PDF, or fail fast.

        Re-uses ``<slug>/<ym>.pdf`` if an operator has already resolved the
        opaque-token CDN URL and dropped the PDF there (the happy path once
        the URL is known). Otherwise raises ``IngestError`` rather than
        guessing a non-derivable URL.
        """
        from mfs import paths

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        raise IngestError(
            "JioBlackRock factsheet for "
            f"{ym} is not cached at {out} and cannot be auto-downloaded: the "
            "factsheet PDF lives behind an auth-gated Strapi document index "
            "(service.jioblackrockamc.com/v1/jiobr/api/disclosure-l3s, "
            "bearer-token required) and is served from jioinvest.cdn.jio.com "
            "under an opaque random-token filename that is not derivable from "
            "the data month. Resolve the URL from the factsheet listing page "
            "(https://www.jioblackrockamc.com/statutory-disclosure/"
            "fund-documents/factsheet) and place the PDF at the path above."
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
                scheme = _scheme_name_from_page(text)
                if not scheme:
                    continue
                m = _PTR_TEXT_RE.search(text)
                if not m:
                    continue
                try:
                    pct = float(m.group(1))
                except ValueError:
                    continue
                # Drop NaN / non-positive — JioBlackRock prints a real percent
                # or omits the block; there is no zero-PTR scheme.
                if pct != pct or pct <= 0:
                    continue
                # Percent -> fraction.
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=pct / 100.0,
                    source_amc=self.amc_slug,
                )

    # -------------------------------------------------------------------
    # Holdings: not extracted here — the Excel-based path is canonical.
    # parse_holdings inherits the base no-op.
    # -------------------------------------------------------------------
