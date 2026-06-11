"""Canara Robeco Mutual Fund monthly portfolio holdings adapter (Phase 5).

Canara Robeco publishes ONE Excel per scheme per month (HDFC/SBI-style, NOT a
consolidated workbook). The monthly-portfolio disclosure page is a WordPress
site:

    https://www.canararobeco.com/documents/statutory-disclosures/
        scheme-dashboard/scheme-monthly-portfolio/

The page LOOKS like a SPA (the document list is empty until you pick a
year+month), but the filter is NOT an AJAX call — the "Submit" handler simply
reloads the SAME page with two extra query params, and WordPress renders the
matching document rows SERVER-SIDE. So a plain ``curl`` of the page with the
filter params already carries every scheme's ``.xlsx`` link as a normal
``<a href>``. The three relevant query params are:

  - ``searchyear``  = Indian FISCAL year of the data month, e.g. data month
    2026-04 → ``2026-27`` (April-anchored FY); 2026-01 → ``2025-26``.
  - ``filteryear``  = the CALENDAR year of the data month (e.g. ``2026``).
  - ``filtermonth`` = the 2-digit CALENDAR month of the data month (``04``).

The listing is paginated (~10 rows/page) via ``&pagination=<n>`` (1-based); we
walk pages until one returns no new ``.xlsx`` links.

Each row's anchor carries:
  - href: ``.../wp-content/uploads/{publish-YYYY}/{publish-MM}/<CODE>-–-Canara
    -Robeco-<Scheme>-–-<Month>-<Year>.xlsx`` — note the folder is the PUBLISH
    month (data + 1, April data lives under .../2026/05/), but the human month
    inside the filename is the DATA month ("April-2026"). We key discovery off
    the filter params, not the filename date, so this offset is irrelevant.
  - link text: ``<CODE> – Canara Robeco <Scheme Name> – April-2026`` (the
    separators are en-dashes U+2013, occasionally a plain hyphen, with
    inconsistent spacing). We strip the leading 2-char code and the trailing
    month tag to recover the printed scheme name (e.g. "Canara Robeco Flexi cap
    Fund").

Per-scheme Excel layout (validated against Canara Robeco Flexicap Fund "DV",
April 2026): a single sheet named after the 2-char scheme code, with the
canonical SEBI header on row 3 — col B "Name of the Instrument", col C "ISIN",
col D "Industry / Rating", col E "Quantity", col F "Market/Fair Value", col G
"% to Net Assets" (in PERCENT units), then section banners ("Equity & Equity
related", "(a) Listed / awaiting listing on Stock Exchanges", "Money Market
Instruments", "Net Current Assets", …) interleaved with ISIN rows.

Because the per-scheme sheet IS the canonical SEBI layout, we reuse the
validated generic single-sheet parser (``parse_sebi_excel`` via the inherited
``GenericHoldingsAdapter.parse_excel``): it auto-detects the header row,
ISIN/name/weight columns, the weight unit, dedupes ISINs, and stops at GRAND
TOTAL. Validated on Flexicap (DV): 69 equity ISIN rows summing to 94.4%. Only
discovery is bespoke.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

import httpx

from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._registry import register_adapter
from mfs.utils.logging import get_logger

log = get_logger(__name__)

_DISCLOSURE_PAGE = (
    "https://www.canararobeco.com/documents/statutory-disclosures/"
    "scheme-dashboard/scheme-monthly-portfolio/"
)

# The disclosure HTML pages 403 the project's default User-Agent
# ('mfs-pipeline/...'); they require a browser UA. (The .xlsx files themselves
# serve to any UA, so fetch_excel can stay on the inherited cached download.)
# We therefore fetch the listing HTML directly with a browser UA via httpx.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def _fetch_html(url: str) -> str:
    """GET a disclosure HTML page with a browser User-Agent (the site 403s the
    pipeline's default UA on these pages)."""
    with httpx.Client(
        timeout=30.0,
        headers={
            "User-Agent": _BROWSER_UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
        },
        follow_redirects=True,
    ) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text

# Anchor → (xlsx URL, link-text label) for monthly-portfolio rows. We accept
# only .xlsx/.xls under the WordPress uploads dir, so unrelated PDFs / nav
# links on the page can never leak in. The label is captured so we can derive
# the printed scheme name from it (more reliable than the filename, whose
# separators/spacing drift).
_ANCHOR_RE = re.compile(
    r'<a[^>]+href="(?P<url>https://www\.canararobeco\.com/wp-content/uploads/'
    r'\d{4}/\d{2}/[^"]+?\.(?:xlsx|xls))"[^>]*>(?P<label>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Strip HTML tags from an anchor's inner text.
_TAG_RE = re.compile(r"<[^>]+>")

# A label looks like "<CODE> – Canara Robeco <Scheme> – April-2026". The code
# is the leading 1-3 char token before the first dash; the trailing
# "<Month>-<Year>" tag is the data-month stamp. Dash chars seen: '-' (U+002D),
# '–' (U+2013 en dash). Spacing around them is inconsistent.
# Dash chars seen in labels/filenames: '-' (U+002D), '–' (U+2013), '—'
# (U+2014). Inside a regex character class the literal hyphen MUST be the FIRST
# char (else it's read as a range operator), so the dashes lead and '\s'
# follows: '[-–—\s]'.
_DASH_WS_CLASS = r"[-–—\s]"
_DASH_ONLY_CLASS = r"[-–—]"
_MONTH_NAMES_RE = (
    "january|february|march|april|may|june|"
    "july|august|september|october|november|december"
)
# Trailing "[-–] <Month>[-–]<Year>" (with optional spaces) at end of label.
_TRAILING_MONTH_RE = re.compile(
    rf"{_DASH_WS_CLASS}+(?:{_MONTH_NAMES_RE}){_DASH_WS_CLASS}*\d{{4}}\s*$",
    re.IGNORECASE,
)
# Leading "<CODE> [-–]" prefix (code is a short alnum token, e.g. DV, MD, ET).
_LEADING_CODE_RE = re.compile(
    rf"^\s*[A-Za-z0-9]{{1,4}}\s*{_DASH_ONLY_CLASS}\s*",
)

# Two schemes are listed on the disclosure page under stale marketing names that
# the SEBI/AMFI scheme_master does NOT carry, so the shared fuzzy matcher binds
# them to the wrong sibling fund (verified: "Flexi cap" → Mid Cap @91.7;
# "Consumer Trends" → GILT @87.8). Each .xlsx's OWN row-0 title is the canonical
# name — e.g. the "Consumer Trends" workbook is titled "CANARA ROBECO
# CONSUMPTION FUND" and the "Flexi cap" workbook "CANARA ROBECO FLEXICAP FUND"
# (confirmed against April-2026 files). We canonicalise the discovered label to
# the workbook title so the matcher resolves it at 100. Keys are lowercased,
# whitespace-collapsed substrings; matched as a phrase within the cleaned label.
#   - "flexi cap"      → "flexicap"   (the AMC spaces the word; scheme_master
#                                      writes it solid). Pure spacing variant.
#   - "consumer trends"→ "consumption" (Canara Robeco rebrand; the workbook
#                                       title and AMFI master both say
#                                       "Consumption").
_LABEL_CANONICAL_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bflexi\s*cap\b", re.IGNORECASE), "Flexicap"),
    (re.compile(r"\bconsumer\s+trends\b", re.IGNORECASE), "Consumption"),
)


def _fiscal_year(ym: str) -> str:
    """data_ym → Indian fiscal-year token used by ``searchyear``.

    The Indian FY runs April→March, so April-2026 data is in FY 2026-27 while
    January-2026 data is in FY 2025-26. Returns e.g. '2026-27'.
    """
    y, m = map(int, ym.split("-"))
    if m >= 4:
        return f"{y}-{(y + 1) % 100:02d}"
    return f"{y - 1}-{y % 100:02d}"


def _scheme_name_from_label(label: str) -> str | None:
    """Recover the printed scheme name from an anchor label.

    'DV – Canara Robeco Flexi cap Fund – April-2026' →
    'Canara Robeco Flexi cap Fund'. Returns None if the cleaned text is empty
    or doesn't look like a scheme name.
    """
    # Unescape entities the listing emits (&amp; etc.) and drop any nested tags.
    import html as _html

    text = _html.unescape(_TAG_RE.sub("", label))
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.lower() == "download":
        return None
    text = _TRAILING_MONTH_RE.sub("", text).strip()
    text = _LEADING_CODE_RE.sub("", text).strip()
    # Defensive: a real Canara Robeco scheme label always contains the AMC name.
    if "canara robeco" not in text.lower():
        return None
    # Map stale marketing labels to the canonical SEBI/AMFI scheme name so the
    # shared matcher binds them to the right scheme_code (see comment above).
    for pat, repl in _LABEL_CANONICAL_SUBS:
        text = pat.sub(repl, text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


@register_adapter
class CanaraRobecoHoldingsAdapter(GenericHoldingsAdapter):
    amc_slug = "canara_robeco"
    source_label = "Canara Robeco Mutual Fund"

    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {printed_scheme_name: absolute .xlsx URL} for data month
        ``ym`` by paginating the server-rendered monthly-portfolio listing.
        """
        y, m = ym.split("-")
        fy = _fiscal_year(ym)
        out: dict[str, str] = {}
        seen_urls: set[str] = set()
        # Walk pagination until a page adds no new xlsx links. Cap at a sane
        # upper bound (Canara Robeco has ~27 schemes ≈ 3 pages) so a listing
        # quirk can't loop forever.
        for page in range(1, 11):
            params = (
                f"?searchyear={fy}&filteryear={y}"
                f"&filtermonth={m}&pagination={page}"
            )
            html = _fetch_html(_DISCLOSURE_PAGE + params)
            page_new = 0
            for mt in _ANCHOR_RE.finditer(html):
                url = mt.group("url")
                if url in seen_urls:
                    continue
                scheme = _scheme_name_from_label(mt.group("label"))
                if not scheme:
                    # The "Download" button is a second anchor on the same row
                    # pointing at the same URL; once we've recorded the URL via
                    # its labelled anchor we skip it here. If we hit the bare
                    # Download anchor first, fall back to the filename.
                    scheme = self._scheme_from_filename(url)
                    if not scheme:
                        continue
                seen_urls.add(url)
                out.setdefault(scheme, url)
                page_new += 1
            if page_new == 0:
                break
        log.info(
            "holdings.canara_robeco.discover",
            ym=ym, fiscal_year=fy, n_schemes=len(out),
        )
        return out

    @staticmethod
    def _scheme_from_filename(url: str) -> str | None:
        """Fallback: derive the scheme name from the xlsx filename when an
        anchor has no usable label (e.g. a bare 'Download' button)."""
        filename = unquote(url.rsplit("/", 1)[-1])
        filename = re.sub(r"\.(?:xlsx|xls)$", "", filename, flags=re.IGNORECASE)
        # Filenames use the same '<CODE>-–-Canara-Robeco-<Scheme>-–-<Month>-Y'
        # shape but with '-' joining tokens; normalise to spaces then reuse the
        # label cleaner.
        text = re.sub(r"[-_]+", " ", filename)
        return _scheme_name_from_label(text)
