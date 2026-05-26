"""Tests for the DSP factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/dsp/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR extraction plus URL
construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.dsp import DspAdapter

PDF = Path(__file__).parent.parent / "data" / "raw" / "factsheets" / "dsp" / "2026-04.pdf"


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026():
    """DSP's canonical /latest-literature/ URL embeds the data month name
    and 4-digit year (no separate publish folder)."""
    a = DspAdapter()
    assert a.build_url("2026-04") == (
        "https://www.dspim.com/latest-literature/dsp-factsheet-april-2026.pdf"
    )


def test_build_url_december_rollover():
    a = DspAdapter()
    assert a.build_url("2026-12") == (
        "https://www.dspim.com/latest-literature/dsp-factsheet-december-2026.pdf"
    )


def test_build_url_january():
    a = DspAdapter()
    assert a.build_url("2027-01") == (
        "https://www.dspim.com/latest-literature/dsp-factsheet-january-2027.pdf"
    )


# ---------------------------------------------------------------------------
# Scheme-name detection (pure unit tests)
# ---------------------------------------------------------------------------


def test_scheme_name_simple():
    text = "DSP Flexi Cap Fund\n(erstwhile known as DSP Equity Fund)\n..."
    assert DspAdapter._scheme_name_from_page(text) == "DSP Flexi Cap Fund"


def test_scheme_name_strips_inline_erstwhile_paren():
    """Some FoF titles fit the whole '(Erstwhile ...)' on line 1 — strip it."""
    text = (
        "DSP Focused Fund (Erstwhile known as DSP Focus Fund)\n"
        "An open ended equity scheme ...\n"
    )
    assert (
        DspAdapter._scheme_name_from_page(text) == "DSP Focused Fund"
    )


def test_scheme_name_strips_wrapped_erstwhile_fragment():
    """Long FoF titles wrap '(Erstwhile' onto line 1 with the close paren
    on line 2 — we strip the dangling fragment."""
    text = (
        "DSP World Mining Overseas Equity Omni FoF (Erstwhile\n"
        "known as DSP World Mining Fund of Fund)\n"
        "..."
    )
    assert (
        DspAdapter._scheme_name_from_page(text)
        == "DSP World Mining Overseas Equity Omni FoF"
    )


def test_scheme_name_strips_dollar_footnote():
    """ELSS Tax Saver carries a '$$' footnote marker that must be trimmed."""
    text = (
        "DSP ELSS Tax Saver Fund (erstwhile known as DSP Tax Saver Fund)$$\n"
        "An open ended equity linked saving scheme ...\n"
    )
    assert (
        DspAdapter._scheme_name_from_page(text) == "DSP ELSS Tax Saver Fund"
    )


def test_scheme_name_rejects_cover_page():
    text = "SEBI Registration No.: MF/036/97/7\nApril 30, 2026"
    assert DspAdapter._scheme_name_from_page(text) is None


def test_scheme_name_rejects_index_page():
    text = "Index\nSr. No Particulars Page No\n01 DSP Flexi Cap Fund 04"
    assert DspAdapter._scheme_name_from_page(text) is None


# ---------------------------------------------------------------------------
# PTR against cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> DspAdapter:
    return DspAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: DspAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached DSP PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """April 2026 PDF carries PTR for ~50 schemes (equity + hybrid +
    most ETFs / index funds). Debt / FoF pages legitimately don't."""
    assert len(ptr_records) >= 1
    # Calibration: 52 in the captured PDF — guard against silent regressions.
    assert len(ptr_records) >= 40


def test_parse_ptr_known_scheme(ptr_records):
    """DSP Flexi Cap Fund prints '0.29' for PTR (Last 12 months) on the
    April 2026 publish. PTR is stored as a fraction — DSP already
    publishes it as one, so no ×100 conversion."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "DSP Flexi Cap Fund" in by_name
    rec = by_name["DSP Flexi Cap Fund"]
    assert rec.ptr == pytest.approx(0.29, abs=1e-6)
    assert rec.source_amc == "dsp"


def test_parse_ptr_hybrid_high_turnover(ptr_records):
    """Hybrid schemes legitimately show high turnover. DSP Equity Savings
    Fund prints 5.39 — guards against accidental ×100 percent encoding
    or under-windowing in the word-position search."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "DSP Equity Savings Fund" in by_name
    assert by_name["DSP Equity Savings Fund"].ptr == pytest.approx(
        5.39, abs=1e-6
    )


def test_parse_ptr_values_positive(ptr_records):
    """Fail-fast: zero / NaN PTR must be dropped at the adapter."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 100, r  # PTR is a fraction, not a percent
