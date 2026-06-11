"""JioBlackRock Mutual Fund monthly portfolio holdings adapter (Phase 5).

JioBlackRock (Jio BlackRock) publishes ONE SEBI-format Excel per scheme per
month. Its statutory-disclosures site (``www.jioblackrockamc.com``) is a
Next.js **App Router** SPA. The Monthly Portfolio Disclosure list is NOT in
the server-rendered HTML — the page ships with ``l3Data:[]`` and the document
list is loaded client-side by a React component (``getDisclosureL3Data``).

Behind that component sits an **auth-gated Strapi CMS**: the client code reads
``STRAPI_API_URL`` / ``STRAPI_API_TOKEN`` from *server-only* environment
variables and attaches ``Authorization: bearer <token>``. Those secrets never
reach the browser, and the underlying Strapi blob host
(``jioinveststorage.blob.core.windows.net/strapi``) 401s anonymously — so the
Strapi API itself is genuinely gated, as the factsheet build agent found.

The unlock is that the document fetch is a **Next.js Server Action**, not a
direct browser->Strapi call. The disclosure-page bundle wires it up as::

    h = createServerReference(
        "70f6a730f357954e4483bbdc8988624b3f73cad662", callServer, ...,
        "getDisclosureL3Data")
    # later, in the L3 list component:
    {data} = await h(l2Id, {year, month, date}, productType)

A Server Action is invoked by POSTing to the page route itself with a
``Next-Action: <action-id>`` header and a JSON-array body of the arguments;
Next.js runs the action ON THE SERVER (where the Strapi token lives) and
streams back the result as a ``text/x-component`` (RSC flight) payload. So we
never need the gated token — the JioBlackRock server holds it and proxies the
call for us. This is a public, unauthenticated endpoint from our side.

Discovery for data month ``ym``:
  POST https://www.jioblackrockamc.com/statutory-disclosure/disclosures/
       monthly-portfolio-disclosure
    Next-Action: 70f6a730f357954e4483bbdc8988624b3f73cad662
    body: ["monthly-portfolio-disclosure",
           {"year":"FI<YYYY>-<YYYY+1>","month":"<MonthName>","date":null},
           "MF"]

  Two filter quirks, both validated against the live April-2026 response:
   - ``year`` is the *fiscal* year with an ``FI`` prefix the UI hides:
     April-2026 data lives under ``"FI2026-2027"`` (Indian FY starts in April,
     so month M of calendar year Y maps to FY [Y..Y+1] for Apr-Dec and
     [Y-1..Y] for Jan-Mar). Passing the bare "2026-2027" returns 0 rows.
   - ``month`` is the full English month name ("April").

  The response's ``1:`` line is JSON ``{"data":[{...docs...}],"meta":{...}}``.
  Each doc has ``title`` (e.g. "JioBlackRock Flexi Cap Fund-Monthly-Portfolio-
  30-04-2026"), ``date`` ("2026-04-30"), and ``file.url`` — an absolute
  ``.xlsx`` on the public Azure-Front-Door CDN
  (``cdnstorage-...azurefd.net/brcms/<opaque>.xlsx``), which downloads with no
  auth. We derive the printed scheme name by stripping the
  "-Monthly-Portfolio-DD-MM-YYYY" suffix from ``title`` and skip the aggregate
  "JioBlackRock Mutual Fund" consolidated workbook (our single-scheme parser
  can't consume it).

The CDN filenames are opaque hashes, so we key the cache file by the printed
scheme name (the orchestrator already does this via ``scheme_filename``).

Excel layout: standard SEBI single-scheme portfolio on sheet 0. The shared
``parse_sebi_excel`` auto-detects the header (ISIN / Name of the Instrument /
% to NAV) and the weight unit (percent here — validated April-2026 Flexi Cap
117 rows / Large Cap 62 rows, each summing to ~99.6). No bespoke parser
needed, so this is a plain ``GenericHoldingsAdapter`` subclass.

If a given ``ym`` returns 0 documents, JioBlackRock simply hasn't published
that month yet (it's a young AMC — the archive starts at Jan-2025).
"""

from __future__ import annotations

import json
import re

import httpx

from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_HOST = "https://www.jioblackrockamc.com"
_DISCLOSURE_PATH = "/statutory-disclosure/disclosures/monthly-portfolio-disclosure"
_DISCLOSURE_URL = _HOST + _DISCLOSURE_PATH

# The Server Action id wired up by the disclosure-page bundle for
# getDisclosureL3Data. If JioBlackRock rebuilds the site this hash can rotate;
# discovery raising 0 docs (vs an HTTP error) is the signal to re-extract it
# from the page's createServerReference(...) call.
_ACTION_ID = "70f6a730f357954e4483bbdc8988624b3f73cad662"

# l2Id of the Monthly Portfolio Disclosure node + product line; both are part
# of the Server Action argument tuple (l2Id, {year, month, date}, productType).
_L2_ID = "monthly-portfolio-disclosure"
_PRODUCT_TYPE = "MF"

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# The aggregate all-schemes workbook — its title is exactly the AMC name with
# no fund qualifier, so it normalizes to "jioblackrock mutual fund".
_SKIP_SCHEME_NAMES = {"jioblackrock mutual fund"}

# "...-Monthly-Portfolio-30-04-2026" trailer on each document title.
_TITLE_SUFFIX_RE = re.compile(
    r"\s*-\s*Monthly[\s-]*Portfolio\s*-\s*\d{1,2}-\d{1,2}-\d{2,4}\s*$",
    re.IGNORECASE,
)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def _fiscal_year(ym: str) -> str:
    """data month ``ym`` -> JioBlackRock's ``FI<FY>`` filter token.

    Indian fiscal year runs Apr->Mar. Calendar month M of year Y maps to:
      - FY [Y .. Y+1]  for Apr..Dec (M >= 4)
      - FY [Y-1 .. Y]  for Jan..Mar (M <= 3)
    e.g. '2026-04' -> 'FI2026-2027'; '2026-02' -> 'FI2025-2026'.
    """
    y, m = map(int, ym.split("-"))
    start = y if m >= 4 else y - 1
    return f"FI{start:04d}-{start + 1:04d}"


def _month_name(ym: str) -> str:
    """'2026-04' -> 'April' (the full month-name value the filter expects)."""
    return _MONTHS[int(ym.split("-")[1]) - 1]


def _scheme_from_title(title: str) -> str:
    """Strip the '-Monthly-Portfolio-DD-MM-YYYY' trailer to get the printed
    scheme name (e.g. 'JioBlackRock Flexi Cap Fund')."""
    name = _TITLE_SUFFIX_RE.sub("", title or "")
    return re.sub(r"\s+", " ", name).strip()


def _parse_flight_data(text: str) -> list[dict]:
    """Extract the document list from a Server Action RSC flight response.

    The response is line-oriented; the payload line is ``1:{...}`` carrying
    ``{"data":[...docs...],"meta":{...}}``. We pull that JSON object and
    return its ``data`` array (empty if the month isn't published).
    """
    for line in text.splitlines():
        # Flight lines look like '<ref>:<json>'. We want the one whose JSON
        # body has a top-level "data" list of documents.
        idx = line.find(":")
        if idx <= 0:
            continue
        body = line[idx + 1:].strip()
        if not body.startswith("{") or '"data"' not in body:
            continue
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        data = obj.get("data")
        if isinstance(data, list):
            return data
    return []


@register_adapter
class JioBlackRockHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "jio_blackrock"
    source_label = "JioBlackRock Mutual Fund"

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {printed_scheme_name: absolute_xlsx_url} for data month
        ``ym`` by invoking the getDisclosureL3Data Next.js Server Action."""
        fy = _fiscal_year(ym)
        month = _month_name(ym)
        body = json.dumps([
            _L2_ID,
            {"year": fy, "month": month, "date": None},
            _PRODUCT_TYPE,
        ])
        headers = {
            "User-Agent": _UA,
            "Accept": "text/x-component",
            "Content-Type": "text/plain;charset=UTF-8",
            "Next-Action": _ACTION_ID,
            "Origin": _HOST,
            "Referer": _DISCLOSURE_URL,
        }
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            r = client.post(_DISCLOSURE_URL, headers=headers, content=body)
            r.raise_for_status()
            docs = _parse_flight_data(r.text)

        out: dict[str, str] = {}
        for doc in docs:
            file_obj = doc.get("file") or {}
            url = (file_obj.get("url") or "").strip()
            if not url or not url.lower().endswith(".xlsx"):
                continue
            scheme = _scheme_from_title(doc.get("title") or "")
            if not scheme or scheme.lower() in _SKIP_SCHEME_NAMES:
                continue
            out.setdefault(scheme, url)

        log.info(
            "holdings.jio_blackrock.discover",
            ym=ym, fiscal_year=fy, month=month,
            n_docs=len(docs), n_schemes=len(out),
        )
        return out
