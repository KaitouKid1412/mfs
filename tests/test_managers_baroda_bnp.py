"""Tests for the Baroda BNP Paribas Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/baroda_bnp/2026-04.pdf``. Tests run entirely
offline — they read the bundled PDF and exercise PTR extraction plus
URL construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.baroda_bnp import (
    BarodaBnpAdapter,
    _NAME_FOF_RE,
    _NAME_SUFFIX_RE,
)
from mfs.ingest.managers._scheme_match import (
    _ADAPTER_SLUG_TO_SCHEME_MASTER_AMC_CODE,
    resolve_scheme_master_amc_code,
)

PDF = (
    Path(__file__).parent.parent
    / "data" / "raw" / "factsheets" / "baroda_bnp" / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url — Baroda BNP returns the CMS permalink (the per-upload PDF
# suffix is non-deterministic; fetch() resolves the actual PDF via HTML
# scraping).
# ---------------------------------------------------------------------------


def test_build_url_returns_permalink():
    """Adapter returns the deterministic permalink (NOT the per-month
    PDF URL, which carries a non-deterministic upload id)."""
    a = BarodaBnpAdapter()
    assert a.build_url("2026-04") == (
        "https://www.barodabnpparibasmf.in/downloads/monthly-factsheet"
    )


def test_build_url_is_stable_across_months():
    """The permalink is the same regardless of data month — fetch() does
    the per-month resolution via HTML scrape."""
    a = BarodaBnpAdapter()
    assert a.build_url("2026-04") == a.build_url("2026-12")
    assert a.build_url("2026-04") == a.build_url("2027-01")


# ---------------------------------------------------------------------------
# Alias registration — the adapter slug doesn't match scheme_master's
# amc_code, so an alias entry is required for fuzzy matching to work.
# ---------------------------------------------------------------------------


def test_amc_slug_alias_registered():
    """`baroda_bnp` must alias to `baroda_bnp_paribas` in scheme_master."""
    assert (
        _ADAPTER_SLUG_TO_SCHEME_MASTER_AMC_CODE["baroda_bnp"]
        == "baroda_bnp_paribas"
    )


def test_resolve_scheme_master_amc_code():
    assert resolve_scheme_master_amc_code("baroda_bnp") == "baroda_bnp_paribas"


# ---------------------------------------------------------------------------
# Name-suffix regex behavior — order matters.
# ---------------------------------------------------------------------------


def test_name_fof_regex_preferred_for_fund_of_fund():
    """``Gold ETF Fund of Fund`` must keep its full suffix, NOT collapse
    to ``Gold ETF`` (which would conflate with the standalone Gold ETF
    scheme)."""
    text = (
        "Baroda BNP Paribas Gold ETF Fund of Fund (An open-ended fund "
        "of fund scheme investing"
    )
    m = _NAME_FOF_RE.match(text)
    assert m is not None
    assert m.group(1) == "Baroda BNP Paribas Gold ETF Fund of Fund"


def test_name_suffix_regex_picks_bare_etf_when_no_fof():
    """``Baroda BNP Paribas Gold ETF`` (standalone) must keep the
    ``ETF`` suffix when no ``Fund of Fund`` follows."""
    text = "Baroda BNP Paribas Gold ETF (An open-ended scheme replicating)"
    assert _NAME_FOF_RE.match(text) is None
    m = _NAME_SUFFIX_RE.match(text)
    assert m is not None
    assert m.group(1) == "Baroda BNP Paribas Gold ETF"


def test_name_suffix_regex_fof_plural():
    """The CMS uses both ``Fund of Fund`` (singular) and ``Fund of
    Funds`` (plural) — both must be preserved end-to-end."""
    text = "Baroda BNP Paribas Income Plus Arbitrage Active Fund of Funds"
    m = _NAME_FOF_RE.match(text)
    assert m is not None
    assert m.group(1) == text


# ---------------------------------------------------------------------------
# PTR against cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> BarodaBnpAdapter:
    return BarodaBnpAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: BarodaBnpAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Baroda BNP PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """April 2026 PDF carries Portfolio Turnover Ratio for ~32 schemes
    (equity / hybrid / arbitrage / index / ETF; pure debt schemes don't
    print the PTR block)."""
    assert len(ptr_records) >= 1
    # Calibration: 32 in the captured PDF — guard against regressions.
    assert len(ptr_records) >= 25


def test_parse_ptr_known_scheme(ptr_records):
    """Baroda BNP Paribas Large Cap Fund prints ``Portfolio Turnover
    Ratio : 0.69`` on April 30, 2026. Baroda BNP stores PTR as a
    fraction — no percent-to-fraction conversion."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Baroda BNP Paribas Large Cap Fund" in by_name
    rec = by_name["Baroda BNP Paribas Large Cap Fund"]
    assert rec.ptr == pytest.approx(0.69, abs=1e-6)
    assert rec.source_amc == "baroda_bnp"


def test_parse_ptr_handles_arbitrage_high_turnover(ptr_records):
    """Arbitrage schemes legitimately show very high turnover. Baroda
    BNP Paribas Arbitrage Fund prints 12.80 on April 30, 2026 (i.e.
    1280%, normal for arbitrage). Guards against accidental clipping."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Baroda BNP Paribas Arbitrage Fund" in by_name
    assert by_name["Baroda BNP Paribas Arbitrage Fund"].ptr == pytest.approx(
        12.80, abs=1e-3
    )


def test_parse_ptr_values_positive(ptr_records):
    """Fail-fast: zero / NaN PTR must be dropped at the adapter."""
    for r in ptr_records:
        assert r.ptr > 0, r
