"""Old Bridge Mutual Fund — factsheet PTR adapter.

Calibrated against the April 2026 combined factsheet
(``data/raw/factsheets/old_bridge/2026-04.pdf``, ~0.8 MB, 14 pages).

URL discovery
-------------
Old Bridge serves factsheets from a Strapi-style CDN under
``/uploads/`` with a RANDOM hash suffix baked into every filename
(e.g. ``OLD_BRIDGE_MF_Factsheet_1667eaa6ab.pdf``,
``Old_Bridge_MF_Mar_26_Factsheet_27c21eb0b4.pdf``). The hash is NOT
derivable from the data month, so a templated ``build_url`` is
impossible. Instead ``build_url`` scrapes the public listing page
``https://www.oldbridgemf.com/factsheet.html`` and resolves the link
whose visible label is the DATA month (e.g. "April 2026"). The label is
the data month, so matching on it transparently absorbs any publish-month
shift (the April 2026 sheet is filed under the "2026-27" section but still
labelled "April 2026"). This keeps the adapter slug-stable across months.

Layout findings driving the parser
----------------------------------
* Each scheme occupies its own page. The printed scheme name is the
  FIRST non-empty line of the page and always begins ``OLD BRIDGE ``
  (e.g. ``OLD BRIDGE FOCUSED FUND^``). The trailing ``^`` footnote
  marker is stripped.

* Only ONE scheme prints PTR in the April 2026 sheet: the Focused Fund.
  It prints as a **fraction** on its own line::

      Equity Turnover: 0.20
      Total Turnover: 0.20

  Storage uses the same fraction convention, so we PASS THROUGH without
  dividing by 100 (0.20 = 20% turnover). We take ``Equity Turnover`` as
  the canonical Phase-2 metric — consistent with the HDFC / SBI reference
  adapters — rather than ``Total Turnover`` (which sums equity + debt +
  derivative turnover).

* The Flexi Cap Fund (inception 04-Mar-2026) prints NO turnover block:
  the page states "Ratios for Old Bridge Flexi Cap Fund are not captured
  since scheme has not yet completed 1 year." The Arbitrage Fund also
  omits PTR (an arbitrage strategy, the metric is meaningless). Both are
  dropped by fail-fast (no label on the page → no record), never
  fabricated.

* Holdings extraction is intentionally left as the base-class default —
  for this AMC holdings come from the separate Excel path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber

from mfs.errors import IngestError
from mfs.ingest.managers._base import ManagerAdapter
from mfs.ingest.managers._registry import register_adapter
from mfs.io.http import fetch_bytes
from mfs.schemas import ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_LISTING_URL = "https://www.oldbridgemf.com/factsheet.html"

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _data_month_label(ym: str) -> str:
    """data month YYYY-MM → visible listing label, e.g. 'April 2026'."""
    y, m = map(int, ym.split("-"))
    return f"{_MONTH_NAMES[m - 1]} {y}"


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------

# Old Bridge prints PTR as a FRACTION: "Equity Turnover: 0.20" (= 20%).
# Storage is a fraction too, so we pass the value through unchanged.
_PTR_TEXT_RE = re.compile(
    r"Equity\s+Turnover\s*:\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name (first non-empty line starting with
    ``OLD BRIDGE ``), or ``None`` for cover / philosophy / disclaimer pages."""
    if not text:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        first = line
        break
    else:
        return None
    if not first.upper().startswith("OLD BRIDGE "):
        return None
    # Strip footnote markers (e.g. trailing '^') and collapse whitespace.
    name = re.sub(r"[\^*†‡#@]", "", first)
    name = re.sub(r"\s+", " ", name).strip()
    return name or None


@register_adapter
class OldBridgeAdapter(ManagerAdapter):
    """Old Bridge Mutual Fund factsheet adapter."""

    amc_slug = "old_bridge"
    source_label = "Old Bridge Mutual Fund"

    # -------------------------------------------------------------------
    # URL
    # -------------------------------------------------------------------

    def build_url(self, ym: str) -> str:
        """Resolve the absolute PDF URL for data month ym='YYYY-MM'.

        Filenames carry a non-derivable random hash, so we scrape the
        listing page and pick the link whose visible label is the data
        month (e.g. 'April 2026'). Each ``/uploads/...pdf`` anchor is
        immediately preceded on the page by that label.
        """
        label = _data_month_label(ym)
        try:
            html = fetch_bytes(_LISTING_URL).decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            raise IngestError(
                f"old_bridge: could not load factsheet listing {_LISTING_URL}: {e}"
            ) from e

        # Pair each uploads-PDF anchor with the visible text immediately
        # before it; the label "<Month> <Year>" sits right before the <a>.
        for m in re.finditer(r'href="([^"]*?/uploads/[^"]*?\.pdf)"', html, re.IGNORECASE):
            window = html[max(0, m.start() - 400):m.start()]
            # Drop the opening anchor tag fragment (e.g. trailing '<a ')
            # that has no closing '>' yet, then strip complete tags.
            window = re.sub(r"<[a-zA-Z][^>]*$", " ", window)
            visible = re.sub(r"<[^>]+>", " ", window)
            visible = re.sub(r"\s+", " ", visible).strip()
            if visible.endswith(label):
                return urljoin(_LISTING_URL, m.group(1))

        raise IngestError(
            f"old_bridge: no factsheet link labelled {label!r} found on "
            f"{_LISTING_URL} (data month {ym})"
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
                    # Scheme legitimately omits PTR (arbitrage funds, and
                    # funds <1yr old whose ratios "are not captured"). Drop
                    # rather than fabricate — half data is worse than none.
                    continue
                try:
                    ptr_value = float(m.group(1))
                except ValueError:
                    continue
                # Drop NaN / non-positive.
                if ptr_value != ptr_value or ptr_value <= 0:
                    continue
                # Fraction in, fraction out — no /100 conversion.
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )
