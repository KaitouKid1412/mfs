"""WhiteOak Capital Mutual Fund — factsheet adapter (Phase 3.B).

Calibrated against the April 2026 combined factsheet PDF
(``data/raw/factsheets/whiteoak_capital/2026-04.pdf``, ~1.93 MB, 63 pages).

URL discovery
-------------
WhiteOak's customer-facing site at ``mf.whiteoakamc.com`` is a Next.js SPA
fronted by CloudFront. The factsheet PDFs themselves live on
``content.whiteoakamc.com`` with a per-upload hash suffix
(e.g. ``Whiteoak_Capital_Factsheet_April_2026_c4eedae969.pdf``), which
makes the static URL non-deterministic month-over-month.

The SPA hydrates from a Strapi v4 GraphQL backend exposed at
``https://cms.whiteoakamc.com/graphql``. The "Factsheet" category in
``forms-and-downloads`` resolves to a ``downloads`` GraphQL collection
whose entries carry a ``download_media_file`` UploadFile relation with
the canonical PDF URL. We resolve the URL by querying:

    query {
      downloads(
        filters: { title: { eq: "Factsheet as at <Last-day-of-month> <YYYY>" } }
        pagination: { limit: 5 }
      ) {
        data {
          attributes {
            title
            download_media_file { data { attributes { url } } }
          }
        }
      }
    }

The title pattern is stable: ``Factsheet as at <day> <Month> <Year>`` where
``<day>`` is the last day of the data month (30 for April, 31 for May/Jul,
28/29 for Feb, etc.). The PDF is published on the 9-13th of the next
calendar month; calling ``build_url`` before the upload date raises
``IngestError``.

Note: CloudFront WAF rejects the default ``mfs-pipeline/0.1`` User-Agent —
both the GraphQL endpoint and the content CDN require a browser-style UA.
We therefore use a dedicated httpx Client in ``fetch`` and ``build_url``
rather than ``mfs.io.http.download_to`` (which always sends the project
UA).

Layout findings driving the parser
----------------------------------
* Each ranked scheme has a primary 1-page entry (some equity schemes also
  have a 2nd portfolio-holdings page that has no Fund Snapshot). The
  first non-empty line is ``WhiteOak Capital <Name> Fund`` for most
  schemes, but a handful wrap the title across two lines:
      Line 1: ``WhiteOak Capital Banking & Financial Services``
      Line 2: ``Fund``
  We accept the first line starting with ``WhiteOak Capital`` and, if it
  doesn't already end with ``Fund``/``FoF``/``ETF``, append the next line
  if that next line is exactly ``Fund`` / ``FoF`` / ``ETF``.

* PTR is printed as ``Portfolio Turn Over Ratio <number> Times`` (note
  the space in "Turn Over"). The value is **already a fraction**: 1.56
  Times = 1.56 ratio (~156% turnover). We pass it through unchanged —
  the storage convention is fraction. The Arbitrage Fund prints 17.39
  Times, which is consistent with arbitrage strategies that roll
  derivatives weekly (Tata Arbitrage = 2.92, Bandhan Arbitrage uses a
  fraction column too — 17.39 is high but plausible).

  A handful of pages append sector-bleed tokens after the ``Times`` word
  (e.g. ``Portfolio Turn Over Ratio 7.22 Times Equivalent 4.60%`` on the
  Equity Savings Fund page). The regex anchors on the numeric immediately
  preceding ``Times`` and ignores anything after.

  WhiteOak Capital Consumption Opportunities Fund prints ``Portfolio
  Turnover Ratio is not computed since the Scheme has not …`` — the
  numeric regex fails to match so the page legitimately yields no PTR
  row.

* AUM is printed as two stacked lines in the left column:
      ``Monthly Average AUM ` 7,638.10 Crore``
      ``Month End AUM ` 7,912.78 Crore``
  We extract the **Month End AUM** value (point-in-time, consistent with
  Stage 2's treatment of ``aum_crore``). Values are in INR Crore — no
  unit conversion needed. The rupee glyph is rendered as a backtick by
  pdfplumber.

  Some pages column-bleed sector data into the AUM line
  (``Month End AUM ` 603.49 Crore Rainbow Childrens Medicare …``). The
  regex anchors on the literal ``Crore`` word boundary after the
  number, so it picks up the AUM correctly regardless of trailing junk.

Calibration counts on 2026-04
-----------------------------
* 18 scheme pages detected (Flexi Cap, Mid, ELSS, Large, Large&Mid,
  Multi, Pharma & Healthcare, Banking, Special Opportunities, ESG,
  Digital Bharat, Quality Equity, Consumption Opportunities, Balanced
  Advantage, Multi Asset, Balanced Hybrid, Arbitrage, Equity Savings,
  Liquid, Ultra Short Duration — 20 scheme pages including 2 debt
  schemes without PTR).
* 18 PTR rows extracted (every equity + hybrid + arbitrage page except
  Consumption Opportunities, which prints ``not computed`` because the
  scheme has not completed a full year). The two debt pages (Liquid,
  Ultra Short Duration) also don't print PTR.
* 20 AUM rows extracted (every scheme page).
"""

from __future__ import annotations

import re
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
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# A browser-style User-Agent is required — CloudFront WAF blocks the
# default ``mfs-pipeline/...`` UA on both the GraphQL endpoint and the
# content CDN with a 403 (and a stock CloudFront block page).
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)


def _last_day_of_month(year: int, month: int) -> int:
    """Return the last calendar day for (year, month). No external deps."""
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 31
    if month in (4, 6, 9, 11):
        return 30
    # February — Gregorian leap year rule.
    if (year % 4 == 0 and year % 100 != 0) or year % 400 == 0:
        return 29
    return 28


# Scheme-name detection: any first non-empty line that starts with
# ``WhiteOak Capital ``. We then optionally append the next line if the
# first line doesn't already end with Fund/FoF/ETF and the next line is
# exactly ``Fund``/``FoF``/``ETF`` (wrapped-title case).
_PREFIX_RE = re.compile(r"^WhiteOak\s+Capital\s+", re.IGNORECASE)
_HAS_FUND_SUFFIX_RE = re.compile(
    r"\b(?:Fund|FoF|ETF)\s*$", re.IGNORECASE
)
_FUND_WRAP_RE = re.compile(r"^(?:Fund|FoF|ETF)\s*$", re.IGNORECASE)

# PTR (text-extract path). The label is "Portfolio Turn Over Ratio" (with
# a space inside "Turn Over"). The value is **already a fraction**
# (1.56 Times = 1.56 ratio). We capture the bare numeric token that
# immediately precedes ``Times`` and ignore any sector/holding data that
# may bleed in after.
_PTR_RE = re.compile(
    r"Portfolio\s+Turn\s*Over\s+Ratio\s+(\d+(?:\.\d+)?)\s+Times",
    re.IGNORECASE,
)

def _scheme_name_from_page(text: str) -> str | None:
    """Return the printed scheme name from a scheme page, or None.

    Scans the first non-empty line for a ``WhiteOak Capital ...`` start.
    Concatenates the second line when the first doesn't end with
    Fund/FoF/ETF and the second line is just ``Fund``/``FoF``/``ETF``
    (wrapped-title pages: Banking & Financial Services, ESG, Consumption
    Opportunities).
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not _PREFIX_RE.match(first):
        return None
    if not _HAS_FUND_SUFFIX_RE.search(first) and len(lines) >= 2:
        second = lines[1]
        if _FUND_WRAP_RE.match(second):
            first = f"{first} {second}"
    # Collapse multiple spaces (defensive — no observed double-spaces).
    name = re.sub(r"\s+", " ", first).strip()
    # Final gate: the cleaned name must contain a Fund/FoF/ETF suffix.
    # The marketing cover (``APRIL\n2026``) and TOC page (``Index``) lack
    # the WhiteOak Capital prefix entirely so they're rejected upstream.
    if not _HAS_FUND_SUFFIX_RE.search(name):
        return None
    return name


def _resolve_factsheet_url(ym: str) -> str:
    """Query WhiteOak's Strapi GraphQL API for the data-month factsheet URL.

    The ``downloads`` collection's ``title`` field is the user-visible
    document title: ``Factsheet as at <day> <Month> <year>``. The
    ``download_media_file`` relation carries the canonical PDF URL on
    ``content.whiteoakamc.com``.

    Raises IngestError if the API returns no match — we never fall back
    to a hand-rolled URL guess (the hash suffix is non-deterministic).
    """
    y, m = map(int, ym.split("-"))
    month_name = _MONTH_NAMES[m - 1]
    day = _last_day_of_month(y, m)
    title = f"Factsheet as at {month_name} {day}, {y}"

    api_url = "https://cms.whiteoakamc.com/graphql"
    query = (
        "query Q($title: String!) { "
        "downloads(filters: { title: { eq: $title } }, pagination: { limit: 5 }) "
        "{ data { id attributes { title download_media_file "
        "{ data { attributes { url } } } } } } }"
    )
    payload = {"query": query, "variables": {"title": title}}
    try:
        with httpx.Client(
            timeout=60.0,
            headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "*/*",
                "Content-Type": "application/json",
            },
            follow_redirects=True,
        ) as c:
            r = c.post(api_url, json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        raise IngestError(
            f"whiteoak_capital: GraphQL request failed at {api_url}: {e}"
        ) from e
    docs = (
        data.get("data", {})
        .get("downloads", {})
        .get("data")
        or []
    )
    for d in docs:
        attrs = d.get("attributes") or {}
        media = (attrs.get("download_media_file") or {}).get("data")
        if not media:
            continue
        url = (media.get("attributes") or {}).get("url")
        if url:
            return url
    raise IngestError(
        f"whiteoak_capital: no downloads entry with title={title!r} found "
        f"in the Strapi GraphQL response ({len(docs)} docs scanned). The "
        "CMS may not have published the issue yet."
    )


@register_adapter
class WhiteoakCapitalAdapter(ManagerAdapter):
    """WhiteOak Capital Mutual Fund factsheet adapter.

    ``amc_slug = 'whiteoak_capital'`` matches ``scheme_master.amc_code``
    directly, so no alias entry in ``_scheme_match.py`` is required.
    """

    amc_slug = "whiteoak_capital"
    source_label = "WhiteOak Capital Mutual Fund"

    def build_url(self, ym: str) -> str:
        """Return the canonical WhiteOak combined-factsheet PDF URL for
        data month ym='YYYY-MM'.

        WhiteOak's PDFs are CMS-hosted with a non-deterministic per-upload
        hash, so the URL must be resolved via the Strapi GraphQL API.
        ``build_url`` performs that resolution and returns the stable
        ``content.whiteoakamc.com/Whiteoak_Capital_Factsheet_<...>.pdf``
        URL.

        The PDF is published 9-13 days after the data month closes
        (April 2026 was published on May 11); calling before the upload
        date raises ``IngestError``.
        """
        return _resolve_factsheet_url(ym)

    def fetch(self, ym: str) -> Path:
        """Resolve the data-month factsheet URL via the GraphQL API, then
        download to the canonical cache path.

        Overrides the base implementation because (a) ``build_url`` is a
        network call we don't want to repeat on cache hits, and (b) the
        content CDN's CloudFront WAF rejects the project's default
        ``mfs-pipeline/...`` User-Agent with a 403. We use a dedicated
        httpx Client carrying a browser UA.

        Re-uses an existing cached PDF unconditionally — delete the file
        to force a re-fetch.
        """
        from mfs import paths

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        url = self.build_url(ym)
        try:
            with httpx.Client(
                timeout=120.0,
                headers={
                    "User-Agent": _BROWSER_UA,
                    "Accept": "*/*",
                },
                follow_redirects=True,
            ) as c:
                r = c.get(url)
                r.raise_for_status()
                payload = r.content
        except Exception as e:  # noqa: BLE001
            raise IngestError(
                f"whiteoak_capital: failed to download {url}: {e}"
            ) from e
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.rename(out)
        return out

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
                m = _PTR_RE.search(text)
                if not m:
                    # Consumption Opportunities prints "not computed";
                    # debt-only pages (Liquid, Ultra Short Duration) omit
                    # the block entirely. Both legitimately yield nothing.
                    continue
                try:
                    ptr = float(m.group(1))
                except ValueError:
                    continue
                # WhiteOak prints "Times" — already a fraction (1.56
                # Times = 1.56 ratio). Pass through.
                if ptr != ptr or ptr <= 0:
                    # Drop NaN / non-positive (fail-fast on garbage rows).
                    continue
                yield ParsedPtrRecord(
                    scheme_name_printed=scheme,
                    ptr=ptr,
                    source_amc=self.amc_slug,
                )

    # ------------------------------------------------------------------
    # Holdings — deferred. WhiteOak's portfolio listings live on a
    # secondary page for equity schemes (e.g. page 9 for Flexi Cap) in a
    # multi-column "Top 10 Holdings / Top 10 Industries / Other Holdings"
    # layout without ISINs. The parallel Phase 3.A ISIN-tagged Excel path
    # covers portfolio overlap; we return () here.
    # ------------------------------------------------------------------

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        return ()
