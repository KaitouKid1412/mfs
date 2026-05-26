"""Tests for the UTI factsheet adapter.

Calibrated against the cached April 2026 "UTI Fund Watch (Active)" publish
at ``data/raw/factsheets/uti/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR / AUM extraction plus URL
construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.uti import (
    UtiAdapter,
    _publish_ym,
    _scheme_name_from_page,
)

PDF = Path(__file__).parent.parent / "data" / "raw" / "factsheets" / "uti" / "2026-04.pdf"


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_uses_publish_month_folder():
    """UTI's CloudFront path uses the PUBLISH month folder (data_month + 1)
    while the filename embeds the data month + 4-digit year."""
    a = UtiAdapter()
    assert a.build_url("2026-04") == (
        "https://d3ce1o48hc5oli.cloudfront.net/s3fs-public/"
        "2026-05/uti_fund_watch_active_april_2026.pdf"
    )


def test_build_url_rolls_year_at_december():
    a = UtiAdapter()
    assert a.build_url("2026-12") == (
        "https://d3ce1o48hc5oli.cloudfront.net/s3fs-public/"
        "2027-01/uti_fund_watch_active_december_2026.pdf"
    )


def test_publish_ym_helper():
    assert _publish_ym("2026-04") == "2026-05"
    assert _publish_ym("2025-12") == "2026-01"


# ---------------------------------------------------------------------------
# Scheme-name detection (pure unit test, no PDF needed)
# ---------------------------------------------------------------------------


def test_scheme_name_strips_trailing_category_tag():
    """UTI's equity-fund pages have 'UTI LARGE CAP FUND Category' as the
    first line — the trailing UI tag must be trimmed."""
    text = "UTI LARGE CAP FUND Category\n(Erstwhile UTI Mastershare Unit Scheme)\n..."
    assert _scheme_name_from_page(text) == "UTI LARGE CAP FUND"


def test_scheme_name_handles_category_first_line():
    """UTI's sectoral/thematic pages put 'Category' on line 1 and the
    actual scheme name on line 2."""
    text = "Category\nUTI INFRASTRUCTURE FUND\nThematic\n..."
    assert _scheme_name_from_page(text) == "UTI INFRASTRUCTURE FUND"


def test_scheme_name_skips_cover_and_media_pages():
    """The 'UTI MUTUAL FUND IN MEDIA' pages must not be parsed as scheme
    pages."""
    text = "UTI MUTUAL FUND IN MEDIA\nSome ad copy here\n..."
    assert _scheme_name_from_page(text) is None


# ---------------------------------------------------------------------------
# PTR / AUM extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> UtiAdapter:
    return UtiAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: UtiAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached UTI PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """The April 2026 PDF carries PTR for all equity + hybrid schemes
    (debt schemes don't report it). We must extract at least one."""
    assert len(ptr_records) >= 1


def test_parse_ptr_known_scheme(ptr_records):
    """Large Cap Fund is a canonical UTI scheme. Its PTR on the April 2026
    publish is 0.41 — extracted via the word-position fallback because the
    equity scheme pages column-bleed mangles `extract_text` output."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "UTI LARGE CAP FUND" in by_name
    rec = by_name["UTI LARGE CAP FUND"]
    assert rec.ptr == pytest.approx(0.41, abs=1e-6)
    assert rec.source_amc == "uti"


def test_parse_ptr_hybrid_text_path(ptr_records):
    """Balanced Advantage Fund's PTR (0.97) lives on the second page of
    the multi-page scheme where extract_text is clean — exercises the
    text-regex fast path."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "UTI BALANCED ADVANTAGE FUND" in by_name
    assert by_name["UTI BALANCED ADVANTAGE FUND"].ptr == pytest.approx(
        0.97, abs=1e-6
    )


def test_parse_ptr_values_are_fraction(ptr_records):
    """UTI prints PTR as a fraction. Arbitrage funds legitimately show high
    turnover (UTI Arbitrage Fund prints 11.22), so the upper bound is
    generous; we just guard against an accidental ×100 percent encoding."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 100, r


# ---------------------------------------------------------------------------


