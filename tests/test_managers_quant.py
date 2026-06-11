"""Tests for the quant Mutual Fund PTR adapter (abridged annual report).

quant omits Portfolio Turnover Ratio from its monthly factsheet, so the
adapter sources PTR from quant's SEBI-mandated abridged annual report. Tests
run entirely offline against the cached report at
``data/raw/annual_reports/quant/2023-24.pdf`` plus pure-unit tests for the
fiscal-year URL logic and the table-cell parsers.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from mfs.ingest.managers.quant import (
    QuantAdapter,
    _clean_ptr,
    _clean_scheme_name,
    _period_end,
)

REPORT = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "annual_reports"
    / "quant"
    / "2023-24.pdf"
)


# ---------------------------------------------------------------------------
# Fiscal-year URL resolution (pure unit tests)
# ---------------------------------------------------------------------------


def test_candidate_fy_labels_april():
    """An April run starts at the FY ending the most recent March (FY ending
    Mar 2026 = '2025-26') and walks back six years, newest first."""
    a = QuantAdapter()
    labels = a._candidate_fy_labels("2026-04")
    assert labels[:4] == ["2025-26", "2024-25", "2023-24", "2022-23"]
    assert len(labels) == 6


def test_candidate_fy_labels_pre_april():
    """A Jan–Mar run hasn't crossed the FY boundary yet, so the most recent
    completed FY-end is the previous March (FY ending Mar 2025 = '2024-25')."""
    a = QuantAdapter()
    assert a._candidate_fy_labels("2026-02")[0] == "2024-25"


def test_build_url_pattern():
    """build_url emits the slug-stable annual-report URL for the newest
    candidate FY (fetch() probes older years as a fallback)."""
    a = QuantAdapter()
    assert a.build_url("2026-04") == (
        "https://quantmutual.com/Admin/disclouser/"
        "Annual-Report__quant-Mutual-Fund_Financial-Year-2025-26.pdf"
    )


# ---------------------------------------------------------------------------
# Table-cell parsers (pure unit tests)
# ---------------------------------------------------------------------------


def test_clean_ptr_clean_value():
    assert _clean_ptr("1.44") == pytest.approx(1.44)


def test_clean_ptr_strips_mangled_space():
    """pdfplumber mangles the previous-FY sub-column ('1 .45'); the parser
    strips the stray space so even a mangled cell parses (we read the clean
    current-FY column, but be robust)."""
    assert _clean_ptr("1 .45") == pytest.approx(1.45)


def test_clean_ptr_dash_and_blank_are_none():
    assert _clean_ptr("-") is None
    assert _clean_ptr("") is None
    assert _clean_ptr(None) is None


def test_clean_scheme_name_collapses_newlines():
    assert _clean_scheme_name("quant BUSINESS\nCYCLE FUND") == "quant BUSINESS CYCLE FUND"


def test_clean_scheme_name_rejects_non_scheme_cells():
    assert _clean_scheme_name("Period ended 31 March 2024") == ""
    assert _clean_scheme_name(None) == ""
    assert _clean_scheme_name("6. Portfolio turnover ratio4") == ""


def test_period_end_reads_latest_fy_from_header():
    """The header lists current FY then previous FY; we stamp the latest."""
    row = ["", "Period\nended 31 March 2024", "Period\nended 31 March 2023"]
    assert _period_end(row, "") == date(2024, 3, 1)


# ---------------------------------------------------------------------------
# PTR against the cached abridged annual report
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> QuantAdapter:
    return QuantAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: QuantAdapter):
    pytest.importorskip("pdfplumber")
    if not REPORT.exists():
        pytest.skip(f"Cached quant annual report not found at {REPORT}")
    return list(adapter.parse_ptr(REPORT, "2026-04"))


def test_parse_ptr_yields_equity_schemes(ptr_records):
    """The FY2023-24 abridged report carries ~19 equity/hybrid schemes with a
    Portfolio Turnover Ratio (the 3 debt schemes print '-' and are skipped)."""
    assert len(ptr_records) >= 15


def test_parse_ptr_known_values(ptr_records):
    """Spot-check current-FY PTR values printed in 'times' (= our fraction
    convention; no /100). Calibrated against the FY2023-24 report."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert by_name["quant SMALL CAP FUND"].ptr == pytest.approx(0.68, abs=1e-6)
    # Legacy/abbreviated report names are normalized to current scheme_master
    # spelling so the fuzzy matcher resolves them at full score.
    assert by_name["quant Flexi Cap Fund"].ptr == pytest.approx(1.16, abs=1e-6)
    assert by_name["quant ESG Integration Strategy Fund"].ptr == pytest.approx(2.75, abs=1e-6)


def test_parse_ptr_skips_discontinued_schemes(ptr_records):
    """Report-only schemes with no current scheme_master counterpart are
    skipped — they would otherwise fuzzy-poach a live sibling (e.g. the
    discontinued 'quant Absolute Fund' scores 86 against 'quant Value Fund')."""
    names = {r.scheme_name_printed for r in ptr_records}
    assert "quant ABSOLUTE FUND" not in names
    assert "quant ACTIVE FUND" not in names


def test_parse_ptr_stamps_fy_end(ptr_records):
    """Every record is stamped at the report's true FY-end (31 Mar 2024),
    read from the table header — not the run's data month."""
    assert ptr_records
    assert all(r.as_of_month == date(2024, 3, 1) for r in ptr_records)


def test_parse_ptr_values_sane(ptr_records):
    """Fail-fast: PTR must be a positive fraction; debt-fund '-' rows dropped."""
    for r in ptr_records:
        assert 0.0 < r.ptr < 20.0, r
        assert r.source_amc == "quant"
