"""Tests for the Mirae Asset factsheet adapter.

Calibrated against the cached April 2026 publish at
``data/raw/factsheets/mirae/2026-04.pdf``. The tests run entirely offline —
they read the bundled PDF and exercise PTR extraction plus URL
construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.mirae import MiraeAdapter, _publish_ym

PDF = Path(__file__).parent.parent / "data" / "raw" / "factsheets" / "mirae" / "2026-04.pdf"


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_uses_publish_month():
    """Mirae names the PDF by the PUBLISH month (data month + 1). Data month
    2026-04 → publish May 2026 → filename ``factsheet-may-2026.pdf``."""
    a = MiraeAdapter()
    assert a.build_url("2026-04") == (
        "https://www.miraeassetmf.co.in/docs/default-source/fachsheet/"
        "factsheet-may-2026.pdf"
    )


def test_build_url_rolls_year_at_december():
    a = MiraeAdapter()
    # data month Dec → publish January next year
    assert a.build_url("2026-12") == (
        "https://www.miraeassetmf.co.in/docs/default-source/fachsheet/"
        "factsheet-january-2027.pdf"
    )


def test_publish_ym_helper():
    assert _publish_ym("2026-04") == "2026-05"
    assert _publish_ym("2025-12") == "2026-01"


# ---------------------------------------------------------------------------
# PTR extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> MiraeAdapter:
    return MiraeAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: MiraeAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Mirae PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """The April 2026 PDF carries PTR for all equity + hybrid schemes
    (debt schemes don't report PTR). We must extract at least one."""
    assert len(ptr_records) >= 1


def test_parse_ptr_known_scheme(ptr_records):
    """Large Cap Fund is a well-known canonical Mirae scheme. Its PTR on the
    April 2026 publish is 0.36 (printed as '0.36 times')."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Mirae Asset Large Cap Fund" in by_name
    rec = by_name["Mirae Asset Large Cap Fund"]
    assert rec.ptr == pytest.approx(0.36, abs=1e-6)
    assert rec.source_amc == "mirae"


def test_parse_ptr_hybrid_word_fallback(ptr_records):
    """Aggressive Hybrid Fund has a column-bleed layout that defeats the text
    regex — the word-position fallback must still recover 0.93."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Mirae Asset Aggressive Hybrid Fund" in by_name
    assert by_name["Mirae Asset Aggressive Hybrid Fund"].ptr == pytest.approx(
        0.93, abs=1e-6
    )


def test_parse_ptr_values_are_fraction(ptr_records):
    """Mirae prints PTR as a fraction (units 'times'), so values should sit
    comfortably below typical percent-form ranges. Arbitrage funds legitimately
    show high turnover (~16.99) so the upper bound is generous; we just guard
    against an accidental ×100 percent encoding."""
    for r in ptr_records:
        assert r.ptr > 0, r
        # 100 == almost certainly an unconverted percent value — flag it.
        assert r.ptr < 100, r
