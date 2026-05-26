"""Tests for the Mahindra Manulife Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined PDF at
``data/raw/factsheets/mahindra_manulife/2026-04.pdf``. Tests run entirely
offline — they read the bundled PDF and exercise PTR / AUM extraction
plus URL construction. No network access is performed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.mahindra_manulife import (
    MahindraManulifeAdapter,
    _scheme_name_from_page,
)

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "mahindra_manulife"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026():
    """The microsite root URL is ``/digital-factsheet/<month-lower>-<YYYY>/
    index.html``; the data month is what's embedded (April 2026 lives at
    ``april-2026``)."""
    a = MahindraManulifeAdapter()
    assert a.build_url("2026-04") == (
        "https://www.mahindramanulife.com/digital-factsheet/april-2026/index.html"
    )


def test_build_url_other_months():
    """The month name is lower-cased and the data year is used verbatim
    — no publish-month shift like Tata/UTI."""
    a = MahindraManulifeAdapter()
    assert a.build_url("2026-12") == (
        "https://www.mahindramanulife.com/digital-factsheet/december-2026/index.html"
    )
    assert a.build_url("2025-01") == (
        "https://www.mahindramanulife.com/digital-factsheet/january-2025/index.html"
    )


# ---------------------------------------------------------------------------
# _scheme_name_from_page (pure unit tests, no PDF dependency)
# ---------------------------------------------------------------------------


def test_scheme_name_standard_layout():
    """Page 5 (Large Cap) layout — title sits between FACTSHEET and the
    parenthetical descriptor."""
    text = (
        "Mahindra Manulife\n"
        "FACTSHEET\n"
        "Large Cap Fund\n"
        "April 2026\n"
        "(Large Cap Fund - An open ended equity scheme\n"
    )
    assert _scheme_name_from_page(text) == "Mahindra Manulife Large Cap Fund"


def test_scheme_name_wrapped_title_with_fund_after_date():
    """Page 10 (Aggressive Hybrid) layout — the trailing ``Fund`` token
    is pushed below the ``April 2026`` line by text-flow reordering;
    we must still pick it up."""
    text = (
        "Mahindra Manulife\n"
        "FACTSHEET\n"
        "Aggressive Hybrid\n"
        "April 2026\n"
        "Fund\n"
        "(An open ended hybrid scheme investing\n"
    )
    assert (
        _scheme_name_from_page(text)
        == "Mahindra Manulife Aggressive Hybrid Fund"
    )


def test_scheme_name_factsheet_first_then_amc():
    """Page 13 (Asia Pacific REITs) layout — ``FACTSHEET`` sorts above
    the AMC line in text-flow order; the title spans two visual rows
    with ``Pacific REITs FOF`` appearing after the date."""
    text = (
        "FACTSHEET\n"
        "Mahindra Manulife Asia\n"
        "April 2026\n"
        "Pacific REITs FOF\n"
        "(An open ended fund of fund scheme investing in Manulife Global Fund –\n"
    )
    assert (
        _scheme_name_from_page(text)
        == "Mahindra Manulife Asia Pacific REITs FOF"
    )


def test_scheme_name_amc_first_part_wrapped():
    """Page 22 (Multi Asset Allocation) layout — the AMC line carries
    the first title word ``Multi`` and the rest wraps to a later
    line."""
    text = (
        "Mahindra Manulife Multi\n"
        "FACTSHEET\n"
        "Asset Allocation Fund\n"
        "April 2026\n"
        "(An open ended scheme investing in Equity, Debt, Gold/Silver\n"
    )
    assert (
        _scheme_name_from_page(text)
        == "Mahindra Manulife Multi Asset Allocation Fund"
    )


def test_scheme_name_returns_none_for_blank():
    assert _scheme_name_from_page("") is None
    assert _scheme_name_from_page("\n\n") is None


def test_scheme_name_returns_none_when_no_mahindra_prefix():
    """Defensive: a non-scheme page (e.g. a cover/glossary) lacking the
    AMC prefix must yield ``None``."""
    text = "Cover Page\nSome other content\n"
    assert _scheme_name_from_page(text) is None


def test_scheme_name_returns_none_when_no_fund_suffix():
    """Title must end in Fund / FOF — guard against partial banners."""
    text = "Mahindra Manulife\nFACTSHEET\nProspectus\nApril 2026\n"
    assert _scheme_name_from_page(text) is None


# ---------------------------------------------------------------------------
# PTR / AUM extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> MahindraManulifeAdapter:
    return MahindraManulifeAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: MahindraManulifeAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Mahindra Manulife PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# ----- PTR ------------------------------------------------------------------


def test_parse_ptr_yields_records(ptr_records):
    """The April 2026 combined PDF carries 15 PTR rows (the equity /
    hybrid / arbitrage schemes that have completed at least one year).
    Floor at 12 — anything below indicates the regex or the
    word-position fallback regressed.
    """
    assert len(ptr_records) >= 12, (
        f"only {len(ptr_records)} PTR records; expected >= 12"
    )


def test_parse_ptr_known_scheme_large_cap(ptr_records):
    """Mahindra Manulife Large Cap Fund prints
    ``Portfolio Turnover Ratio (Last one year): 0.58`` on its detail
    page. The value is stored as a fraction (no percent → fraction
    conversion needed)."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Mahindra Manulife Large Cap Fund" in by_name
    rec = by_name["Mahindra Manulife Large Cap Fund"]
    assert rec.ptr == pytest.approx(0.58, abs=1e-6)
    assert rec.source_amc == "mahindra_manulife"


def test_parse_ptr_values_are_fraction(ptr_records):
    """Mahindra Manulife stores PTR as fraction directly. Even the
    highest-churn schemes (e.g. Arbitrage Fund at PTR ~ 8.88) are well
    below the "this is clearly a percent we forgot to divide" threshold
    of 50."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 50, r


def test_parse_ptr_all_mahindra_branded(ptr_records):
    """Every PTR row must come from a page whose printed scheme name
    starts with the Mahindra Manulife AMC prefix — defends against the
    scheme-name gate leaking a non-scheme page (TOC / cover banner)."""
    for r in ptr_records:
        assert r.scheme_name_printed.startswith("Mahindra Manulife"), r
        assert r.source_amc == "mahindra_manulife", r


# ----- AUM ------------------------------------------------------------------


# ----- Cross-signal sanity --------------------------------------------------


