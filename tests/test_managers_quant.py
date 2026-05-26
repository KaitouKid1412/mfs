"""Tests for the quant Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/quant/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise AUM extraction plus URL
construction.

PTR is intentionally NOT tested for values — the quant factsheet does not
print Portfolio Turnover Ratio per scheme (their stated policy on pages
11 and 74 of the April 2026 publish). ``parse_ptr`` deliberately yields
nothing; the test asserts that contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.quant import QuantAdapter

PDF = Path(__file__).parent.parent / "data" / "raw" / "factsheets" / "quant" / "2026-04.pdf"


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026_uses_hyphen():
    """quant's CMS uses a one-off hyphen between 'Factsheet' and 'April'
    only for the April issue — confirmed by HEAD probe (the underscore
    variant returns 404). The 2026-04 URL therefore differs from every
    other month's naming."""
    a = QuantAdapter()
    assert a.build_url("2026-04") == (
        "https://www.quantmutual.com/Admin/Factsheet/"
        "quant_Factsheet-April_2026.pdf"
    )


def test_build_url_may_uses_underscore():
    """Every non-April month uses the underscore separator."""
    a = QuantAdapter()
    assert a.build_url("2026-05") == (
        "https://www.quantmutual.com/Admin/Factsheet/"
        "quant_Factsheet_May_2026.pdf"
    )


def test_build_url_march_uses_underscore():
    a = QuantAdapter()
    assert a.build_url("2026-03") == (
        "https://www.quantmutual.com/Admin/Factsheet/"
        "quant_Factsheet_March_2026.pdf"
    )


def test_build_url_december():
    a = QuantAdapter()
    assert a.build_url("2026-12") == (
        "https://www.quantmutual.com/Admin/Factsheet/"
        "quant_Factsheet_December_2026.pdf"
    )


# ---------------------------------------------------------------------------
# Scheme-name detection (pure unit tests)
# ---------------------------------------------------------------------------


def test_scheme_name_lowercase_quant_prefix():
    """The AMC brand is always lowercase; first non-empty line is the
    scheme title."""
    from mfs.ingest.managers.quant import _scheme_name_from_page

    text = "quant Small Cap Fund\nInvestment Objective: ...\n"
    assert _scheme_name_from_page(text) == "quant Small Cap Fund"


def test_scheme_name_rejects_capitalized_quant():
    """A title-case ``Quant ...`` line is rejected — the only place
    title-case appears in the factsheet is on cover/metadata pages, not
    on scheme snapshot pages."""
    from mfs.ingest.managers.quant import _scheme_name_from_page

    assert _scheme_name_from_page("Quant Active Equity Fund\n...") is None


def test_scheme_name_rejects_continuation_page():
    """quant Multi Asset Allocation Fund's portfolio spills onto page 65
    whose first line is a GOI bond entry. The Fund/FOF word gate must
    reject this so it doesn't produce a phantom AUM row."""
    from mfs.ingest.managers.quant import _scheme_name_from_page

    text = "Total MFU 0.03\n6.92% GOI 18-Nov-2039 1.33\n..."
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_blank():
    from mfs.ingest.managers.quant import _scheme_name_from_page

    assert _scheme_name_from_page("") is None
    assert _scheme_name_from_page("\n\n") is None


def test_scheme_name_multi_asset():
    """The longer-titled schemes still match — ``quant Multi Asset
    Allocation Fund`` has Fund as the last token."""
    from mfs.ingest.managers.quant import _scheme_name_from_page

    text = (
        "quant Multi Asset Allocation Fund\n"
        "Investment Objective: ...\n"
    )
    assert (
        _scheme_name_from_page(text) == "quant Multi Asset Allocation Fund"
    )


# ---------------------------------------------------------------------------
# AUM / PTR against cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> QuantAdapter:
    return QuantAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: QuantAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached quant PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# ----- PTR contract: empty, by design --------------------------------------


def test_parse_ptr_yields_nothing(ptr_records):
    """quant deliberately omits PTR from the factsheet (pages 11/74 in
    the April-2026 issue make this explicit). The adapter must NOT
    fabricate values; ``parse_ptr`` returns an empty iterable.
    """
    assert ptr_records == []


# ----- AUM extraction -------------------------------------------------------


