"""quant Mutual Fund — factsheet adapter (Phase 3.B).

Calibrated against the April 2026 combined factsheet PDF
(``data/raw/factsheets/quant/2026-04.pdf``, ~11.78 MB, 80 pages, 26 equity/
hybrid + 3 debt scheme detail pages plus cover / philosophy / glossary /
"how to read the factsheet" / dividend history / PoS locations at the back).

URL discovery
-------------
The AMC site (``www.quantmutual.com``) is a classic ASP.NET WebForms app.
The factsheet downloads page at ``/downloads/factsheet`` lists per-month
PDFs hosted under ``/Admin/Factsheet/`` with a slug-stable filename. The
April-2026 issue uses a one-off naming quirk — a HYPHEN between
``Factsheet`` and ``April`` — while every other month uses an underscore:

    https://www.quantmutual.com/Admin/Factsheet/quant_Factsheet_May_2026.pdf
    https://www.quantmutual.com/Admin/Factsheet/quant_Factsheet-April_2026.pdf  <- April only
    https://www.quantmutual.com/Admin/Factsheet/quant_Factsheet_March_2026.pdf
    https://www.quantmutual.com/Admin/Factsheet/quant_Factsheet_February_2026.pdf

The April underscore variant returns 404 — confirmed by HEAD probe — so
``build_url(ym)`` emits the hyphen filename for April and the underscore
filename for every other month. Both convention branches embed the **data
month** (no publish-month shift like Tata / Franklin / UTI). The filename
is otherwise deterministic and slug-stable across months.

Layout findings driving the parser
----------------------------------
* Each scheme has two consecutive pages — a narrative "manager commentary"
  page (no AUM/PTR markers) followed by a "scheme snapshot" page that
  prints the FUND SIZE block in the top-right corner. We anchor on the
  FUND SIZE label and extract the value from the snapshot page only; the
  commentary page is naturally skipped because it doesn't contain
  ``FUND SIZE``.

* Scheme name lives on the first non-empty line of the snapshot page and
  starts with the lowercase prefix ``quant `` (e.g. ``quant Small Cap
  Fund``, ``quant ELSS Tax Saver Fund``, ``quant Multi Asset Allocation
  Fund``). One scheme — ``quant Multi Asset Allocation Fund`` — has its
  portfolio table spill onto a third page (page 65) whose first line is a
  GOI bond entry (``Total MFU 0.03``). That continuation page has no
  FUND SIZE so it's naturally skipped (and would yield a non-quant first
  line if it did).

* AUM is printed in the top-right corner of the snapshot page as two
  stacked rows:
      ``FUND SIZE``                       (label, x0 ~ 525, y ~ 67)
      ``₹ 25,821 cr``                     (INR Crore value, y ~ 82)
      ``$ 2.73 bn``                       (USD billions display, y ~ 90)
  We prefer the INR Crore row (it's already in our storage convention).
  pdfplumber's ``extract_text`` mangles this block on most pages because
  the FUND SIZE label is fused into the wrap of the Investment Objective
  paragraph in the text-flow layout (see e.g. page 14:
  ``...long-term growth FUND SIZE``). Word-position extraction handles
  this cleanly — the ₹ glyph is its own token at x0 ~ 525, the numeric
  body is at x0 ~ 535, and the ``cr`` unit is at x0 ~ 550. We anchor on
  the top-right ``FUND`` label (x0 > 470, y < 130), then take the first
  ``cr``-terminated numeric token below it.

* **PTR is NOT printed in the factsheet** — quant deliberately omits the
  Portfolio Turnover Ratio per scheme and instead prints Sharpe / Sortino
  / Jensen's Alpha. Page 11 of the factsheet contains an essay arguing
  that PTR is "an irrelevant measure", and page 74 ("How to read the
  factsheet") repeats the stance. The "RISK ADJUSTED MEASURES" snapshot
  block on each equity scheme page shows Sharpe / Sortino / Jensen's
  Alpha / R-Squared / Downside / Upside Deviation / Capture ratios —
  no turnover figure anywhere. Manual entry / cross-source fabrication
  is forbidden by the project's fail-fast invariants ("No manual entry,
  no nullable strict fields"), so ``parse_ptr`` deliberately yields
  nothing for quant. PTR ingestion for quant is deferred to a parallel
  data source (AMFI portal monthly portfolio statement) if/when such a
  source is integrated in a future phase.

Calibration counts on 2026-04
-----------------------------
* 29 scheme snapshot pages detected (26 equity/hybrid + 3 debt:
  Liquid / Gilt / Overnight)
* 29 AUM rows extracted (every snapshot page yields a clean
  ``₹ <value> cr`` token)
* 0 PTR rows extracted (intentional — see "PTR is NOT printed" above)
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


def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from the first non-empty line, or
    ``None`` if this page is not a scheme snapshot.

    The factsheet detail pages start with the scheme title on line 1
    (e.g. ``quant Small Cap Fund``). Cover / philosophy / TOC / glossary /
    "How to read the factsheet" pages either start with something else or
    are blank-text (full-page images). We require the leading token to be
    the literal lowercase ``quant`` to filter aggressively — the AMC's
    own branding rule is that the company name is always lowercase, and
    the only place ``Quant`` (title-case) appears in the PDF is in the
    page-title metadata for legacy scheme footers, which we don't read.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    # Must start with the lowercase brand prefix. We do NOT accept
    # title-case ``Quant `` here — the cover page banner uses that and
    # is not a scheme page.
    if not first.startswith("quant "):
        return None
    # Reject prose / TOC entries — a real scheme title is short and
    # ends with ``Fund`` (or ``Fund of Fund`` / ``FOF``). The continuation
    # pages don't have FUND SIZE markers anyway so the AUM gate later
    # would drop them.
    if not re.search(r"\b(?:Fund|FOF|Fund\s+of\s+Fund)\b", first, re.IGNORECASE):
        return None
    # Collapse whitespace defensively.
    return re.sub(r"\s+", " ", first).strip()


@register_adapter
class QuantAdapter(ManagerAdapter):
    """quant Mutual Fund factsheet adapter.

    ``amc_slug = 'quant'`` matches ``scheme_master.amc_code`` exactly
    (verified against the 130 quant schemes in the master table), so no
    alias entry in ``_scheme_match.py`` is required. Note the lowercase
    branding — that is the AMC's actual name, not a typo.
    """

    amc_slug = "quant"
    source_label = "quant Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical quant combined-factsheet PDF URL for data
        month ym='YYYY-MM'.

        quant publishes the factsheet under ``/Admin/Factsheet/`` with a
        slug-stable filename embedding the data month name and four-digit
        year. There is no publish-month shift (unlike Tata / Franklin /
        UTI). One naming quirk on the CMS — the April-2026 issue uses a
        HYPHEN between ``Factsheet`` and ``April`` (``quant_Factsheet-
        April_2026.pdf``) while every other month uses an underscore
        (``quant_Factsheet_May_2026.pdf``). The underscore variant for
        April returns 404 — confirmed by HEAD probe — so we branch on
        the month here.
        """
        y, m = map(int, ym.split("-"))
        month_name = _MONTH_NAMES[m - 1]
        separator = "-" if m == 4 else "_"
        return (
            "https://www.quantmutual.com/Admin/Factsheet/"
            f"quant_Factsheet{separator}{month_name}_{y}.pdf"
        )

    # ------------------------------------------------------------------
    # PTR / AUM
    # ------------------------------------------------------------------

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        """Yield nothing — quant Mutual Fund deliberately omits the
        Portfolio Turnover Ratio from its factsheet.

        Page 11 of the April-2026 factsheet contains an essay arguing
        PTR is "an irrelevant measure"; page 74 ("How to read the
        factsheet") repeats the stance. The scheme snapshot blocks
        publish Sharpe / Sortino / Jensen's Alpha / R-Squared /
        Downside / Upside Deviation / Capture ratios instead. PTR
        ingestion for quant is therefore deferred — the project's
        fail-fast invariant forbids manual entry or cross-source
        fabrication, so we return an empty iterable rather than
        invent values.
        """
        return ()

    # ------------------------------------------------------------------
    # Holdings — deferred. The quant snapshot pages print the top-10
    # holdings inline as a two-column ``<Company Name> <% to NAV>``
    # block but without ISINs; Phase 3.A's parallel ISIN-tagged Excel
    # path covers quant for the portfolio overlap metric, so we return
    # () here.
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
