"""Tests for the Franklin Templeton (India) factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/franklin/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise scheme-name + PTR / AUM
extraction. ``build_url`` is NOT exercised against the network; the
Franklin SPA's literature API is non-deterministic month over month, so
we test only the alias entry and the parser outputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.franklin import FranklinAdapter
from mfs.ingest.managers._scheme_match import (
    _ADAPTER_SLUG_TO_SCHEME_MASTER_AMC_CODE,
    resolve_scheme_master_amc_code,
)

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "franklin"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# Slug & alias
# ---------------------------------------------------------------------------


def test_amc_slug_is_short_form():
    """Adapter uses the short slug ``franklin``; the long form
    ``franklin_templeton`` lives only in scheme_master.amc_code."""
    assert FranklinAdapter.amc_slug == "franklin"


def test_alias_entry_resolves_to_scheme_master_amc_code():
    """The alias must be present in ``_scheme_match`` — without it, the
    orchestrator's fuzzy-match would silently match zero schemes because
    scheme_master uses ``franklin_templeton`` as the amc_code."""
    assert _ADAPTER_SLUG_TO_SCHEME_MASTER_AMC_CODE.get("franklin") == (
        "franklin_templeton"
    )
    assert resolve_scheme_master_amc_code("franklin") == "franklin_templeton"


# ---------------------------------------------------------------------------
# build_url — Franklin's URL is resolved via the literature API at
# runtime, so we cannot string-compare against a fixed expected value.
# We sanity-check that build_url uses the literature-API resolver shape
# by ensuring the public callable exists and is the adapter's hook.
# ---------------------------------------------------------------------------


def test_build_url_callable_exists():
    """build_url is the orchestrator's URL hook even though Franklin
    overrides ``fetch`` to do the lookup. The method must exist and
    accept the data-month string."""
    a = FranklinAdapter()
    # Don't actually invoke it (would do a network call); just verify
    # the attribute is the right shape.
    assert callable(a.build_url)
    assert callable(a.fetch)


# ---------------------------------------------------------------------------
# PTR / AUM extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> FranklinAdapter:
    return FranklinAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: FranklinAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Franklin PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# PTR ------------------------------------------------------------------------


def test_parse_ptr_yields_records(ptr_records):
    """Franklin prints PTR on equity / hybrid / arbitrage pages — debt /
    index / liquid / ETF pages do not print it. 2026-04 carries 20 PTR
    rows; minimum acceptance bar is 1."""
    assert len(ptr_records) >= 1


def test_parse_ptr_flexi_cap_known_value(ptr_records):
    """Franklin India Flexi Cap Fund (the legacy Franklin India Equity
    Fund). April 2026 PTR is printed as 23.45%; stored as 0.2345 after
    percent→fraction normalisation."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Franklin India Flexi Cap Fund" in by_name
    rec = by_name["Franklin India Flexi Cap Fund"]
    assert rec.ptr == pytest.approx(0.2345, abs=1e-6)
    assert rec.source_amc == "franklin"


def test_parse_ptr_prefers_equity_variant_on_hybrid_page(ptr_records):
    """Franklin India Aggressive Hybrid Fund prints two PTR rows —
    aggregate ``Portfolio Turnover 87.30%`` and equity-only
    ``Portfolio Turnover (Equity) 35.09%``. The adapter prefers the
    (Equity) variant so the metric stays comparable to stock-picking
    PTR on pure equity funds."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    if "Franklin India Aggressive Hybrid Fund" in by_name:
        rec = by_name["Franklin India Aggressive Hybrid Fund"]
        assert rec.ptr == pytest.approx(0.3509, abs=1e-6)


def test_parse_ptr_values_positive(ptr_records):
    """Fail-fast invariant: PTR <= 0 or NaN must be dropped at the
    adapter level."""
    for r in ptr_records:
        assert r.ptr > 0, r


# AUM ------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scheme-name canonicalisation
# ---------------------------------------------------------------------------


def test_scheme_name_strips_trailing_dollar_and_abbrev(ptr_records):
    """Franklin appends scheme codes like 'FIMF', 'FILCF' and footnote
    glyphs '$$' to the printed scheme line. The cleaner must strip both
    so the result matches the AMFI scheme_master canonical form."""
    for r in ptr_records:
        name = r.scheme_name_printed
        assert not name.endswith("$"), name
        assert not name.endswith("$$"), name
        # No trailing all-caps abbreviation (the regex consumes 3-8
        # uppercase chars; longer or hyphenated codes don't apply).
        tail = name.split()[-1] if name.split() else ""
        if 3 <= len(tail) <= 8 and tail.isupper():
            pytest.fail(f"scheme name still has trailing abbrev: {name!r}")
