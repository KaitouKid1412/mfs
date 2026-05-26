"""Tests for the Canara Robeco Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/canara_robeco/2026-04.pdf``. Tests run entirely
offline — they read the bundled PDF and exercise PTR / AUM extraction.

``build_url`` is tested via a stubbed transport so the test stays
offline; the resolver scrapes Canara Robeco's listing page which would
otherwise require a network request. The stubbed listing HTML mirrors
the real anchor markup observed in May 2026.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.errors import IngestError
from mfs.ingest.managers.canara_robeco import (
    CanaraRobecoAdapter,
    _extract_ptr,
    _publish_ym,
    _scheme_name_from_page,
)

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "canara_robeco"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url — stub the network transport so the test is offline-friendly
# ---------------------------------------------------------------------------


def _stub_listing_html(month: str, year: int, pdf_url: str) -> str:
    """Build a minimal listing-page HTML payload that contains exactly one
    matching anchor for the target month/year."""
    return (
        f'<html><body><a class="download-btn" href="{pdf_url}">'
        f"Factsheet – {month} {year}"
        "</a></body></html>"
    )


def test_build_url_resolves_via_listing_page(monkeypatch):
    """build_url scrapes the listing page and returns the anchor URL whose
    label matches ``Factsheet – <Month> <YYYY>`` for the target data
    month. We stub httpx so the test is offline."""
    expected = (
        "https://www.canararobeco.com/wp-content/uploads/2026/05/"
        "Canara-Robeco-factsheet-as-on-April-2026.pdf"
    )
    html = _stub_listing_html("April", 2026, expected)

    class _StubResponse:
        text = html

        def raise_for_status(self):
            return None

    class _StubClient:
        def __init__(self, *a, **kw):
            self._html = html

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kw):
            return _StubResponse()

    import httpx

    monkeypatch.setattr(httpx, "Client", _StubClient)
    adapter = CanaraRobecoAdapter()
    assert adapter.build_url("2026-04") == expected


def test_build_url_raises_when_month_missing(monkeypatch):
    """If the listing page lacks an anchor for the target month/year the
    resolver must raise IngestError rather than guess a URL."""
    html = _stub_listing_html("March", 2026, "https://example.com/x.pdf")

    class _StubResponse:
        text = html

        def raise_for_status(self):
            return None

    class _StubClient:
        def __init__(self, *a, **kw):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kw):
            return _StubResponse()

    import httpx

    monkeypatch.setattr(httpx, "Client", _StubClient)
    adapter = CanaraRobecoAdapter()
    with pytest.raises(IngestError):
        adapter.build_url("2026-04")


# ---------------------------------------------------------------------------
# Pure unit tests (no I/O)
# ---------------------------------------------------------------------------


def test_publish_ym_rolls_year_at_december():
    """Publish month = data month + 1; December rolls to next January."""
    assert _publish_ym("2026-04") == (2026, 5)
    assert _publish_ym("2026-12") == (2027, 1)


def test_scheme_name_strips_trailing_footnote_marker():
    """``CANARA ROBECO LARGE AND MID CAP FUND*`` (with a trailing star
    footnote) should yield the clean name without the star."""
    text = (
        "CANARA ROBECO LARGE AND MID CAP FUND*\n"
        "(An open ended equity scheme investing across large cap and mid cap stocks)\n"
        "as on April 30, 2026\n"
    )
    assert (
        _scheme_name_from_page(text)
        == "CANARA ROBECO LARGE AND MID CAP FUND"
    )


def test_scheme_name_returns_none_for_non_scheme_page():
    """Performance summary / TOC / market-review pages start with
    something other than ``CANARA ROBECO`` and must return None."""
    text = (
        "Performance for all Schemes - Regular Plan\n"
        "as on April 30, 2026\n"
    )
    assert _scheme_name_from_page(text) is None


def test_extract_ptr_prefers_equity_variant_on_hybrid():
    """Hybrid pages print TWO PTR rows — ``Portfolio Turnover Ratio
    (Equity) 0.10 times`` and ``Portfolio Turnover Ratio 1.35 times``.
    The Equity variant represents stock-picking churn and is preferred."""
    text = (
        "Portfolio Turnover Ratio (Equity) 0.10 times Scheme Riskometer\n"
        "Portfolio Turnover Ratio 1.35 times Moderate Risk\n"
    )
    assert _extract_ptr(text) == pytest.approx(0.10, abs=1e-6)


def test_extract_ptr_pure_equity_plain_label():
    """Pure-equity pages print a single ``Portfolio Turnover Ratio <num>
    times`` row without the ``(Equity)`` qualifier."""
    text = "Portfolio Turnover Ratio 0.17 times This product is suitable\n"
    assert _extract_ptr(text) == pytest.approx(0.17, abs=1e-6)


def test_extract_ptr_returns_none_when_label_absent():
    """Debt / liquid pages omit the PTR label entirely — the extractor
    must return None rather than fabricating a value."""
    text = (
        "Month end Assets Under Management (AUM)# `108.49Crores\n"
        "Monthly AVG Assets Under Management (AAUM) `107.81Crores\n"
    )
    assert _extract_ptr(text) is None


# ---------------------------------------------------------------------------
# PTR / AUM extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> CanaraRobecoAdapter:
    return CanaraRobecoAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: CanaraRobecoAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Canara Robeco PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# ----- PTR --------------------------------------------------------------


def test_parse_ptr_yields_records(ptr_records):
    """Canara Robeco publishes PTR for equity / hybrid / ELSS / sector
    pages — 16 rows on the April-2026 PDF. Brief target: >= 12."""
    assert len(ptr_records) >= 12


def test_parse_ptr_known_scheme_large_cap(ptr_records):
    """Canara Robeco Large Cap Fund prints
    ``Portfolio Turnover Ratio 0.17 times`` on page 15 of the April-2026
    PDF. Stored as a fraction (= 0.17, no /100 conversion)."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "CANARA ROBECO LARGE CAP FUND" in by_name
    rec = by_name["CANARA ROBECO LARGE CAP FUND"]
    assert rec.ptr == pytest.approx(0.17, abs=1e-6)
    assert rec.source_amc == "canara_robeco"


def test_parse_ptr_known_scheme_mid_cap(ptr_records):
    """Canara Robeco Mid Cap Fund — 0.48 = 48% turnover — exercises a
    higher-churn equity scheme."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "CANARA ROBECO MID CAP FUND" in by_name
    assert by_name["CANARA ROBECO MID CAP FUND"].ptr == pytest.approx(
        0.48, abs=1e-6
    )


def test_parse_ptr_hybrid_uses_equity_variant(ptr_records):
    """Canara Robeco Conservative Hybrid Fund (page 38) prints two PTR
    rows — ``(Equity) 0.10`` and ``1.35`` (Total). The adapter must
    select the Equity variant so the value reflects stock-picking churn
    rather than the debt-component roll."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "CANARA ROBECO CONSERVATIVE HYBRID FUND" in by_name
    rec = by_name["CANARA ROBECO CONSERVATIVE HYBRID FUND"]
    assert rec.ptr == pytest.approx(0.10, abs=1e-6)
    # Negative check: never silently grab the Total variant.
    assert rec.ptr != pytest.approx(1.35, abs=1e-6)


def test_parse_ptr_skips_pure_debt_schemes(ptr_records):
    """Pure debt / liquid / overnight / gilt schemes legitimately omit
    the PTR block in the factsheet — they must NOT appear in PTR rows
    (fail-fast: no fabricated zeros)."""
    names = {r.scheme_name_printed for r in ptr_records}
    assert "CANARA ROBECO LIQUID FUND" not in names
    assert "CANARA ROBECO OVERNIGHT FUND" not in names
    assert "CANARA ROBECO GILT FUND" not in names
    assert "CANARA ROBECO CORPORATE BOND FUND" not in names


def test_parse_ptr_values_are_fraction(ptr_records):
    """Canara Robeco prints PTR in 'times' (a fraction). Storage is also
    fraction, so no scaling. Sanity-bound to (0, 100) to guard against
    an accidental ×100 percent encoding."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 100, r


# ----- AUM --------------------------------------------------------------


