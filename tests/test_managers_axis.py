"""Tests for the Axis Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/axis/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise scheme-name + PTR extraction
plus URL construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.axis import AxisAdapter

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "axis"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026_matches_downloaded_pdf():
    """The build_url for the calibrated 2026-04 data month must equal the
    URL we actually downloaded from. Encoded space is %20; no
    publish-month offset (the filename embeds the DATA month)."""
    a = AxisAdapter()
    assert a.build_url("2026-04") == (
        "https://www.axismf.com/cms/sites/default/files/pdf-factsheets/"
        "Axis%20Fund%20Factsheet%20April%202026.pdf"
    )


def test_build_url_rolls_month_name():
    """URL pattern is stable; only the month name and year shift."""
    a = AxisAdapter()
    assert a.build_url("2026-05") == (
        "https://www.axismf.com/cms/sites/default/files/pdf-factsheets/"
        "Axis%20Fund%20Factsheet%20May%202026.pdf"
    )
    assert a.build_url("2025-12") == (
        "https://www.axismf.com/cms/sites/default/files/pdf-factsheets/"
        "Axis%20Fund%20Factsheet%20December%202025.pdf"
    )


def test_amc_slug_matches_scheme_master_amc_code():
    """No alias entry needed — adapter slug == scheme_master.amc_code."""
    assert AxisAdapter.amc_slug == "axis"


# ---------------------------------------------------------------------------
# PTR extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> AxisAdapter:
    return AxisAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: AxisAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Axis PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# PTR ------------------------------------------------------------------------


def test_parse_ptr_yields_records(ptr_records):
    """Axis only prints PTR on actively-managed equity scheme pages (not
    on debt / index / ETF / FOF / hybrid pages). The April 2026 PDF
    carries 16 such schemes; minimum acceptance bar is 1."""
    assert len(ptr_records) >= 1


def test_parse_ptr_large_cap_known_value(ptr_records):
    """Axis Large Cap Fund is the flagship; April 2026 PTR is 0.77 (77%).
    Already a fraction in source — no /100 normalization."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Axis Large Cap Fund" in by_name
    rec = by_name["Axis Large Cap Fund"]
    assert rec.ptr == pytest.approx(0.77, abs=1e-6)
    assert rec.source_amc == "axis"


def test_parse_ptr_values_are_fraction(ptr_records):
    """Axis prints PTR as a fraction. Values like 2.98 (Axis Quant Fund)
    are legitimate; this guard is against an accidental ×100 percent
    encoding bug."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 100, r
