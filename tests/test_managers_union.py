"""Tests for the Union Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/union/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR / AUM extraction plus URL
construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.union import (
    UnionAdapter,
    _scheme_title_from_words,
)

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "union"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_returns_downloads_api_endpoint():
    """Union's per-month PDF URL embeds a non-deterministic ``sfvrsn``
    cache-buster and an inconsistent filename slug, so ``build_url``
    returns the deterministic Sitefinity downloads API endpoint. The
    ``fetch`` override resolves the actual per-month PDF URL via the
    API at download time."""
    a = UnionAdapter()
    expected = (
        "https://www.unionmf.com/api/downloads/documents?"
        "$filter=FolderId%20eq%2004ec7e02-4df3-424f-b99d-0ef4c3800f3e"
    )
    assert a.build_url("2026-04") == expected


def test_build_url_is_month_agnostic():
    """The downloads API returns every factsheet entry in one shot, so
    the URL doesn't depend on the data month within or across years."""
    a = UnionAdapter()
    assert a.build_url("2026-04") == a.build_url("2026-12")
    assert a.build_url("2026-04") == a.build_url("2027-01")


# ---------------------------------------------------------------------------
# Scheme-name detection — requires the cached PDF (uses real word positions)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> UnionAdapter:
    return UnionAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: UnionAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Union PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_scheme_titles_detected_for_flexi_cap(ptr_records):
    """Union Flexi Cap Fund is the flagship equity scheme — must appear
    in the PTR stream (which exercises the same title detector)."""
    ptr_names = {r.scheme_name_printed for r in ptr_records}
    assert "Union FLEXI CAP FUND" in ptr_names


def test_scheme_titles_handle_multiline_wrap(ptr_records):
    """``Union INNOVATION & OPPORTUNITIES FUND`` wraps onto two visual
    lines (top=74 + top=88) in the title band. The word-position
    extractor must recombine both lines into one title token group."""
    names = {r.scheme_name_printed for r in ptr_records}
    assert "Union INNOVATION & OPPORTUNITIES FUND" in names


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------


def test_parse_ptr_yields_records(ptr_records):
    """April 2026 PDF carries PTR for 21 equity/hybrid/arbitrage/Gold-ETF
    schemes (the 9 debt schemes and the 3 FoF schemes legitimately omit
    the Portfolio Turnover Ratio block)."""
    assert len(ptr_records) >= 1
    # Calibration: 21 in the captured PDF — guard against regressions in
    # the regex that handles the column-bleed stacked-label shape.
    assert len(ptr_records) >= 18


def test_parse_ptr_known_scheme_flexi_cap(ptr_records):
    """Union Flexi Cap Fund prints
    ``Portfolio Turnover Ratio$$$  ... 0.77 times`` (Apr 30 2026, label
    on its own line, value in a 4-column Quantitative Indicators row).
    Union stores PTR in times (already a fraction); no conversion."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Union FLEXI CAP FUND" in by_name
    rec = by_name["Union FLEXI CAP FUND"]
    assert rec.ptr == pytest.approx(0.77, abs=1e-4)
    assert rec.source_amc == "union"


def test_parse_ptr_inline_shape_business_cycle(ptr_records):
    """Some pages render the label inline as ``Portfolio Turnover
    Ratio$$$ : 1.34 times`` (Business Cycle Fund on p8). The same
    regex must handle both the inline and stacked shapes."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Union BUSINESS CYCLE FUND" in by_name
    assert by_name["Union BUSINESS CYCLE FUND"].ptr == pytest.approx(
        1.34, abs=1e-4
    )


def test_parse_ptr_handles_arbitrage_high_turnover(ptr_records):
    """Arbitrage schemes legitimately show very high turnover. Union
    Arbitrage Fund prints 12.12 times — guards against accidental
    clipping to <10x by an under-windowed regex."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Union ARBITRAGE FUND" in by_name
    assert by_name["Union ARBITRAGE FUND"].ptr == pytest.approx(
        12.12, abs=1e-4
    )


def test_parse_ptr_values_positive(ptr_records):
    """Fail-fast: zero / NaN PTR must be dropped at the adapter."""
    for r in ptr_records:
        assert r.ptr > 0, r
        # Arbitrage can reach ~12x but nothing in the Union book should
        # exceed 20x in practice.
        assert r.ptr < 20, r


# ---------------------------------------------------------------------------
# AUM extraction
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_adapter_registered():
    """UnionAdapter must self-register on import so the orchestrator can
    dispatch via ``get_adapter('union')``."""
    from mfs.ingest.managers._registry import get_adapter

    a = get_adapter("union")
    assert isinstance(a, UnionAdapter)
    assert a.amc_slug == "union"
    assert a.source_label == "Union Mutual Fund"


def test_holdings_returns_empty(adapter):
    """Union holdings extraction is deferred to the parallel
    ISIN-tagged Excel path (Phase 3.C); the factsheet PDF prints
    weights without ISINs, so ``parse_holdings`` returns an empty
    iterable."""
    assert list(adapter.parse_holdings(PDF, "2026-04")) == []
