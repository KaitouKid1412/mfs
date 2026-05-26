"""Tests for the Tata Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/tata/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR extraction plus URL
construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.tata import (
    TataAdapter,
    _publish_ym,
    _scheme_name_from_page,
)

PDF = Path(__file__).parent.parent / "data" / "raw" / "factsheets" / "tata" / "2026-04.pdf"


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026():
    """Tata's canonical URL puts the file under the publish-month folder
    (data + 1) and embeds the data month name + 4-digit year in the
    filename, with spaces URL-encoded as %20."""
    a = TataAdapter()
    assert a.build_url("2026-04") == (
        "https://betacms.tatamutualfund.com/system/files/2026-05/"
        "Tata%20MF%20Fact%20Sheet%20-%20April%202026.pdf"
    )


def test_build_url_december_rollover():
    """Data month December 2026 publishes in January 2027, so the
    folder rolls into the next calendar year."""
    a = TataAdapter()
    assert a.build_url("2026-12") == (
        "https://betacms.tatamutualfund.com/system/files/2027-01/"
        "Tata%20MF%20Fact%20Sheet%20-%20December%202026.pdf"
    )


def test_build_url_january():
    a = TataAdapter()
    assert a.build_url("2027-01") == (
        "https://betacms.tatamutualfund.com/system/files/2027-02/"
        "Tata%20MF%20Fact%20Sheet%20-%20January%202027.pdf"
    )


def test_publish_ym_helper():
    assert _publish_ym("2026-04") == (2026, 5)
    assert _publish_ym("2025-12") == (2026, 1)


# ---------------------------------------------------------------------------
# Scheme-name detection (pure unit tests — no PDF required)
# ---------------------------------------------------------------------------


def test_scheme_name_title_case():
    """Most Tata scheme pages have a clean title-case first line."""
    text = "Tata Large Cap Fund\n(An open-ended equity scheme ...)\n..."
    assert _scheme_name_from_page(text) == "Tata Large Cap Fund"


def test_scheme_name_accepts_all_caps():
    """A subset of Tata pages (index / ETF / a few hybrids) print the
    title in ALL CAPS — they must match the same regex."""
    text = "TATA NIFTY 50 INDEX FUND\n..."
    assert _scheme_name_from_page(text) == "TATA NIFTY 50 INDEX FUND"


def test_scheme_name_accepts_fof_suffix():
    """FoF schemes end in ``FOF`` rather than ``Fund``."""
    text = "Tata Income Plus Arbitrage Active FOF\n..."
    assert (
        _scheme_name_from_page(text) == "Tata Income Plus Arbitrage Active FOF"
    )


def test_scheme_name_accepts_etf_suffix():
    text = "Tata Gold Exchange Traded Fund\n..."
    assert (
        _scheme_name_from_page(text) == "Tata Gold Exchange Traded Fund"
    )


def test_scheme_name_rejects_toc_entry():
    """The TOC page prints lines like ``Tata India Consumer Fund 55``
    (scheme + page number). We must NOT treat those as scheme pages."""
    text = "Tata India Consumer Fund 55\nMARKET OUTLOOK\n..."
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_cover():
    """Page 1 is a generic ``FLEXI CAP FUND`` cover without the Tata
    prefix — must NOT be matched."""
    text = "April 2026\nFLEXI CAP FUND\n..."
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_empty():
    assert _scheme_name_from_page("") is None
    assert _scheme_name_from_page("\n\n") is None


# ---------------------------------------------------------------------------
# PTR against cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> TataAdapter:
    return TataAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: TataAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Tata PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """April 2026 PDF carries PTR for ~30 schemes (equity / hybrid /
    arbitrage / most index / ETF pages). Pure-debt / FoF schemes
    legitimately don't print the PTR block, and the FOF ``Tata Income
    Plus Arbitrage Active FOF`` prints ``NA`` so it's excluded."""
    assert len(ptr_records) >= 1
    # Calibration: 32 in the captured PDF — guard against regressions.
    assert len(ptr_records) >= 25


def test_parse_ptr_known_scheme(ptr_records):
    """Tata Large Cap Fund prints ``Portfolio Turnover (Equity component
    only) : 50.03%`` on April 30, 2026. Tata stores PTR as a percent,
    so the adapter divides by 100 to keep the fraction storage
    convention (50.03% -> 0.5003)."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Tata Large Cap Fund" in by_name
    rec = by_name["Tata Large Cap Fund"]
    assert rec.ptr == pytest.approx(0.5003, abs=1e-6)
    assert rec.source_amc == "tata"


def test_parse_ptr_handles_arbitrage_high_turnover(ptr_records):
    """Arbitrage schemes legitimately show very high turnover. Tata
    Arbitrage Fund prints 291.63% (i.e. 2.9163 fraction) — guards
    against accidental clipping to <100% or under-windowing in the
    word-position search."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Tata Arbitrage Fund" in by_name
    assert by_name["Tata Arbitrage Fund"].ptr == pytest.approx(
        2.9163, abs=1e-6
    )


def test_parse_ptr_values_positive(ptr_records):
    """Fail-fast: zero / NaN PTR must be dropped at the adapter."""
    for r in ptr_records:
        assert r.ptr > 0, r
        # PTR is a fraction; arbitrage strategies can reach >5x but
        # nothing in the Tata book exceeds 10x in practice.
        assert r.ptr < 10, r
