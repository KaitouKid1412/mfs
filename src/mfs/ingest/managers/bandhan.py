"""Bandhan Mutual Fund (formerly IDFC Mutual Fund) — factsheet adapter.

Calibrated against the April 2026 combined factsheet PDF
(``data/raw/factsheets/bandhan/2026-04.pdf``, 3.99 MB, 129 pages, ~78 scheme
pages plus market-commentary / disclosure / IDCW history at the back).

URL discovery
-------------
Bandhan publishes the monthly factsheet via its WordPress CMS at
``cmsnew.bandhanmutual.com``. The published PDF lives on Google Cloud
Storage under ``storage.googleapis.com/nonprod-static-assets-<bucket>/``
with a per-upload UUID-style prefix (e.g.
``66818a8b-bandhan-factsheet-april-2026.pdf``), which makes the
GCS URL non-deterministic month-over-month.

The CMS exposes a stable permalink:
  https://cmsnew.bandhanmutual.com/monthly-factsheets-<publish-year>/
  bandhan-factsheet-<datamonth>-<datayear>/
and a JSON listing endpoint:
  https://cmsnew.bandhanmutual.com/wp-json/finance-api/v1/posts/
  monthly-factsheets?posts_per_page=20

``build_url`` returns the deterministic permalink (the CMS post that hosts
the file). ``fetch`` overrides the default behavior to resolve the
permalink HTML to the actual GCS PDF URL before downloading.

The URL pattern accommodates the IDFC→Bandhan rename automatically:
legacy IDFC files (pre-Aug 2023) live on ``cmsnew.bandhanmutual.com/
wp-content/uploads/...`` under ``IDFC-Factsheet-<Month>-<Year>.pdf``; the
CMS listing transparently surfaces those under their year posts. The
``fetch`` resolver scans the HTML for any ``*.pdf`` link, so legacy
filenames work without code change.

Layout findings driving the parser
----------------------------------
* Scheme pages begin with a first line like ``Bandhan Large Cap Fund``
  (optionally followed by ``Click here to Know more`` from a hyperlink
  banner). Footnote symbols (``££``, ``¥``, ``§``, ``$``, ``¢``,
  ``ßßß``, …) are tagged on at the end of the scheme name and are
  stripped after the canonical Fund/FOF/ETF suffix. We require the
  cleaned name to end with ``Fund``, ``FOF`` or ``ETF`` so the back-cover
  ``Bandhan AMC Offices`` page is rejected.

* PTR is printed as ``Portfolio Turnover Ratio\nEquity 0.67\nAggregate
  0.69`` — already a fraction, NOT a percent. We always pick the
  ``Equity`` line (representative of stock-picking activity) and ignore
  ``Aggregate`` (which includes debt/derivatives rotation and can be
  10-20× higher for hybrid / arbitrage schemes). The text-extract layout
  for some pages (e.g. Balanced Advantage) interleaves portfolio table
  cells immediately after ``Portfolio Turnover Ratio`` — we therefore
  search within a 400-char window after the label and anchor on the
  literal ``Equity <number>`` token.

* AUM is printed on two consecutive lines:
    ``Monthly Avg AUM: `1,974.89Crores``
    ``Month end AUM:  `2,007.27Crores``
  We prefer **Month-End AUM** (point-in-time, consistent with Stage 2's
  treatment of ``aum_crore``). On a handful of pages (e.g. Arbitrage Fund
  page 1, Equity Savings Fund) pdfplumber's ``extract_text`` column-bleeds
  the FUND FEATURES block with the holdings table so ``Month end AUM``
  appears mangled in plain text. For those we fall back to
  ``extract_words`` and find the ``Month`` ``end`` ``AUM:`` sequence by
  position, taking the next numeric word as the value. If neither path
  resolves, we accept ``Monthly Avg AUM`` as a last resort and log it
  via the ``source_amc`` provenance.

* All values in INR Crore — no unit conversion needed.

* Page 32 is the Arbitrage Fund portfolio CONTINUATION page (still
  starts with ``Bandhan Arbitrage Fund``); it has no FUND FEATURES
  block. It yields no PTR/AUM so it falls through silently. The
  orchestrator's PK-dedupe drops the duplicate scheme/month tuple if
  both p31 and p32 produced rows.

* The legacy ``IDFC `` prefix is also accepted in the first-line check,
  since archived IDFC-era factsheets retain that header and the same
  parser runs against them unchanged.

Calibration counts on 2026-04
-----------------------------
* 78 scheme pages detected
* 36 PTR rows extracted (equity + hybrid + index funds; debt and arbitrage
  schemes don't print Portfolio Turnover Ratio)
* 77 AUM rows extracted (every scheme page except the Arbitrage portfolio
  continuation page)
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
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def _publish_ym(data_ym: str) -> tuple[int, int]:
    """Publish month = data month + 1 (with year rollover)."""
    y, m = map(int, data_ym.split("-"))
    if m == 12:
        return y + 1, 1
    return y, m + 1


# Match the page header. Accept either Bandhan or legacy IDFC prefix so
# the same parser runs against archived IDFC-era factsheets.
_PREFIXES = ("Bandhan ", "IDFC ")

# Scheme-name normaliser. Captures everything up to the first occurrence
# of ``Fund``/``FOF``/``ETF`` (word boundary), then optionally drops a
# parenthetical scrip-code suffix used on ETF pages, then drops any
# trailing run of footnote glyphs (£ ¥ § $ @ ¢ ^ ß *).
_SCHEME_NAME_RE = re.compile(
    r"^((?:Bandhan|IDFC)\s+.*?(?:Fund|FOF|ETF))(?:\s*\(.*?\))?",
    re.IGNORECASE,
)

# PTR (text-extract path). PTR is printed as
# ``Portfolio Turnover Ratio\nEquity 0.67\nAggregate^ 0.69``.
# We anchor on the label then look for ``Equity <number>`` within a
# 400-char window. Some pages (Balanced Advantage) interleave the
# portfolio cell ``Equity Total 66.87%`` immediately after the label, so
# we must NOT match that — the ``\bEquity\s+\d+\.\d+\b`` anchor is
# preceded by a strict ``\bEquity\s+`` (no ``Total``/``Futures`` word).
_PTR_HEADER_RE = re.compile(r"Portfolio\s+Turnover\s+Ratio", re.IGNORECASE)
_PTR_EQUITY_RE = re.compile(r"\bEquity\s+(\d+\.\d+)\b")


def _scheme_name_from_page(text: str) -> str | None:
    """Return the cleaned scheme title from the first non-empty line, or
    None for non-scheme pages (cover / commentary / IDCW history / back
    cover).
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not any(first.startswith(p) for p in _PREFIXES):
        return None
    # Strip trailing "Click here to Know more" hyperlink banner.
    first = re.sub(r"\s+Click here.*$", "", first, flags=re.IGNORECASE).strip()
    # Strip trailing "An open-ended..." category descriptor.
    first = re.sub(r"\s+An open[- ]ended.*$", "", first, flags=re.IGNORECASE).strip()
    m = _SCHEME_NAME_RE.search(first)
    if not m:
        return None
    return m.group(1).strip()


@register_adapter
class BandhanAdapter(ManagerAdapter):
    """Bandhan Mutual Fund factsheet adapter.

    ``amc_slug`` matches ``scheme_master.amc_code`` directly, so no alias
    entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "bandhan"
    source_label = "Bandhan Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical CMS permalink for data month ym='YYYY-MM'.

        Bandhan's CMS publishes the April-2026 factsheet under the
        2026 monthly-factsheets index with the slug
        ``bandhan-factsheet-april-2026``. The same convention applies
        retroactively to legacy IDFC-era filings (pre-Aug 2023) — the
        CMS slug uses the Bandhan brand even when the PDF inside is
        named ``IDFC-Factsheet-...``.

        The publish year is data_year + (1 if data_month == 12 else 0)
        — Bandhan organises its annual archive page by publish year.
        """
        y, m = map(int, ym.split("-"))
        publish_year, _ = _publish_ym(ym)
        month_lower = _MONTH_NAMES[m - 1]
        return (
            f"https://cmsnew.bandhanmutual.com/monthly-factsheets-{publish_year}/"
            f"bandhan-factsheet-{month_lower}-{y}/"
        )

    def fetch(self, ym: str) -> Path:
        """Download Bandhan's monthly factsheet for data month ym.

        Bandhan's actual PDF lives on ``storage.googleapis.com`` under a
        per-upload UUID-prefixed filename, so the GCS URL cannot be
        constructed deterministically. Instead, we GET the CMS permalink
        (HTML) and scan for the first embedded ``*.pdf`` link, which is
        always the file the page is built around.

        Re-uses an existing cached PDF unconditionally — delete the file
        to force a re-fetch.
        """
        from mfs import paths
        from mfs.io.http import download_to, fetch_bytes

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out

        permalink = self.build_url(ym)
        try:
            html_bytes = fetch_bytes(permalink)
        except Exception as e:  # noqa: BLE001
            raise IngestError(
                f"bandhan: failed to fetch CMS permalink {permalink}: {e}"
            ) from e
        html = html_bytes.decode("utf-8", errors="replace")
        pdf_urls = re.findall(
            r"https?://[^\"'\s<>\\]+?\.pdf",
            html,
        )
        # Filter for the bandhan factsheet (exclude unrelated PDF links
        # on the page like investor charters, KIMs, etc.).
        pdf_urls = [
            u for u in pdf_urls
            if "factsheet" in u.lower() and "passive" not in u.lower()
        ]
        if not pdf_urls:
            raise IngestError(
                f"bandhan: no factsheet PDF link found on permalink "
                f"{permalink} (data month {ym}). The CMS layout may have "
                "changed — please re-discover."
            )
        return download_to(pdf_urls[0], out)

    # -----------------------------------------------------------------
    # PTR
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
                ptr_value = self._extract_ptr(text)
                if ptr_value is None:
                    continue
                if ptr_value != ptr_value or ptr_value <= 0:
                    # NaN or non-positive → drop (fail-fast on garbage).
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr_value,
                    source_amc=self.amc_slug,
                )

    @staticmethod
    def _extract_ptr(text: str) -> float | None:
        """Return the Equity PTR fraction printed on this scheme page, if
        any. PTR is already a fraction in Bandhan factsheets — no
        percent-to-fraction conversion."""
        m_label = _PTR_HEADER_RE.search(text)
        if not m_label:
            return None
        # Search within a 400-char window after the label so we don't
        # pick up a stray ``Equity 9.31`` from an unrelated portfolio
        # table further down the page.
        slice_after = text[m_label.end() : m_label.end() + 400]
        m_val = _PTR_EQUITY_RE.search(slice_after)
        if not m_val:
            return None
        try:
            return float(m_val.group(1))
        except ValueError:
            return None

    # -----------------------------------------------------------------
    # Holdings — deferred. Bandhan's two-column portfolio layout has the
    # same column-bleed issues as HDFC/Kotak and would need its own
    # x-band calibration; Phase 3.A will tackle this. For now we rely on
    # the parallel Excel-based ISIN-tagged path.
    # -----------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
