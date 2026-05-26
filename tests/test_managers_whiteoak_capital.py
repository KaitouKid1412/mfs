"""Tests for the WhiteOak Capital Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/whiteoak_capital/2026-04.pdf``. Tests run entirely
offline — they read the bundled PDF and exercise PTR / AUM extraction
plus the scheme-name regex. ``build_url`` is asserted via string
equality against the resolved URL (no network call).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.whiteoak_capital import (
    WhiteoakCapitalAdapter,
    _last_day_of_month,
    _scheme_name_from_page,
)

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "whiteoak_capital"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026(monkeypatch):
    """``build_url`` calls the Strapi GraphQL backend to resolve the
    non-deterministic CDN URL. We monkeypatch the resolver to assert the
    title pattern + return URL contract, rather than hit the network in
    a unit test.
    """
    from mfs.ingest.managers import whiteoak_capital as mod

    captured = {}

    def fake_resolve(ym: str) -> str:
        captured["ym"] = ym
        return (
            "https://content.whiteoakamc.com/"
            "Whiteoak_Capital_Factsheet_April_2026_c4eedae969.pdf"
        )

    monkeypatch.setattr(mod, "_resolve_factsheet_url", fake_resolve)
    a = WhiteoakCapitalAdapter()
    assert a.build_url("2026-04") == (
        "https://content.whiteoakamc.com/"
        "Whiteoak_Capital_Factsheet_April_2026_c4eedae969.pdf"
    )
    assert captured["ym"] == "2026-04"


def test_last_day_of_month_helper():
    """The GraphQL title embeds the last calendar day of the data
    month. The helper must produce 30 for April, 31 for January /
    May / July / etc, and 28/29 for February (leap-year aware)."""
    assert _last_day_of_month(2026, 4) == 30
    assert _last_day_of_month(2026, 5) == 31
    assert _last_day_of_month(2026, 2) == 28  # 2026 is NOT a leap year
    assert _last_day_of_month(2024, 2) == 29  # 2024 IS a leap year
    assert _last_day_of_month(2026, 12) == 31


# ---------------------------------------------------------------------------
# Scheme-name detection (pure unit tests — no PDF required)
# ---------------------------------------------------------------------------


def test_scheme_name_clean_title():
    """Most WhiteOak scheme pages have a clean first-line title."""
    text = "WhiteOak Capital Flexi Cap Fund\nAn Open Ended ...\n..."
    assert _scheme_name_from_page(text) == "WhiteOak Capital Flexi Cap Fund"


def test_scheme_name_handles_two_line_wrap():
    """Three scheme pages wrap the title across two lines:
      Line 1: ``WhiteOak Capital Banking & Financial Services``
      Line 2: ``Fund``
    The parser must concatenate when line 1 doesn't already end with
    Fund/FoF/ETF and line 2 is exactly ``Fund``/``FoF``/``ETF``."""
    text = (
        "WhiteOak Capital Banking & Financial Services\n"
        "Fund\n"
        "An Open-ended Equity Scheme Investing in Banking & Financial Services Sector"
    )
    assert (
        _scheme_name_from_page(text)
        == "WhiteOak Capital Banking & Financial Services Fund"
    )


def test_scheme_name_handles_esg_wrap():
    """ESG Best-In-Class Strategy also wraps."""
    text = (
        "WhiteOak Capital ESG Best-In-Class Strategy\n"
        "Fund\n"
        "An Open-ended Equity Scheme Investing in Companies Following ..."
    )
    assert (
        _scheme_name_from_page(text)
        == "WhiteOak Capital ESG Best-In-Class Strategy Fund"
    )


def test_scheme_name_rejects_cover():
    """The cover page (``APRIL\\n2026``) lacks the WhiteOak Capital
    prefix and must NOT be matched."""
    text = "APRIL\n2026"
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_index():
    """The TOC page first line is just ``Index`` — not a scheme."""
    text = "Index\nContent Page No.\n..."
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_empty():
    assert _scheme_name_from_page("") is None
    assert _scheme_name_from_page("\n\n") is None


def test_scheme_name_requires_fund_suffix():
    """Defensive: a line beginning ``WhiteOak Capital`` but never
    ending in Fund/FoF/ETF (and with no wrap-Fund follow-up) should be
    rejected — that's not a scheme page."""
    text = (
        "WhiteOak Capital Asset Management Limited\n"
        "Some non-scheme content\n"
    )
    assert _scheme_name_from_page(text) is None


# ---------------------------------------------------------------------------
# PTR / AUM against cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> WhiteoakCapitalAdapter:
    return WhiteoakCapitalAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: WhiteoakCapitalAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached WhiteOak PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """April 2026 PDF carries PTR for ~18 schemes (every equity + hybrid
    + arbitrage page except Consumption Opportunities which prints "not
    computed"). Calibration count = 18 — guard against regressions."""
    assert len(ptr_records) >= 1
    assert len(ptr_records) >= 15


def test_parse_ptr_known_scheme(ptr_records):
    """WhiteOak Capital Flexi Cap Fund prints ``Portfolio Turn Over
    Ratio 1.56 Times`` on April 30, 2026. WhiteOak prints PTR as a
    fraction-equivalent ratio (Times), so the adapter passes the value
    through unchanged."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "WhiteOak Capital Flexi Cap Fund" in by_name
    rec = by_name["WhiteOak Capital Flexi Cap Fund"]
    assert rec.ptr == pytest.approx(1.56, abs=1e-6)
    assert rec.source_amc == "whiteoak_capital"


def test_parse_ptr_handles_arbitrage_high_turnover(ptr_records):
    """Arbitrage schemes legitimately show very high turnover.
    WhiteOak Capital Arbitrage Fund prints 17.39 Times — guards
    against accidental clipping or under-windowing."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "WhiteOak Capital Arbitrage Fund" in by_name
    assert by_name["WhiteOak Capital Arbitrage Fund"].ptr == pytest.approx(
        17.39, abs=1e-6
    )


def test_parse_ptr_values_positive(ptr_records):
    """Fail-fast: zero / NaN PTR must be dropped at the adapter."""
    for r in ptr_records:
        assert r.ptr > 0, r
        # Arbitrage strategies can push very high, but nothing else in
        # the WhiteOak book exceeds 20x in practice.
        assert r.ptr < 25, r


# ---------------------------------------------------------------------------


