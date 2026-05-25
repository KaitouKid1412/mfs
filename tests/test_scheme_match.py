"""Tests for the scheme-name fuzzy matcher (Phase 2.1)."""

from __future__ import annotations

from datetime import date

import polars as pl

from mfs.ingest.managers._scheme_match import (
    DEFAULT_THRESHOLD,
    build_candidate_index,
    canonicalize,
    match_one,
)


def _sm_row(
    code: str,
    name: str,
    amc: str = "hdfc",
    plan: str = "DIRECT",
    option: str = "GROWTH",
    active: bool = True,
):
    """Minimal scheme_master row."""
    return {
        "scheme_code": code,
        "isin_growth": None,
        "isin_idcw": None,
        "scheme_name": name,
        "amc_name": amc,
        "amc_code": amc,
        "plan_type": plan,
        "option_type": option,
        "amfi_category": None,
        "canonical_category": None,
        "benchmark_ticker": None,
        "inception_date": date(2010, 1, 1),
        "base_fund_id": code,
        "is_active": active,
        "last_seen_date": date(2026, 5, 1),
    }


# ---------------------------------------------------------------------------
# canonicalize — should strip plan/option suffix, punctuation, normalize case
# ---------------------------------------------------------------------------


def test_canonicalize_strips_direct_plan_growth_option():
    raw = "HDFC TOP 100 FUND - DIRECT PLAN - GROWTH OPTION"
    assert canonicalize(raw) == "HDFC TOP 100 FUND"


def test_canonicalize_strips_lowercase_variants():
    assert canonicalize("HDFC Flexi Cap Fund - Direct Plan - Growth") == "HDFC FLEXI CAP FUND"


def test_canonicalize_handles_extra_whitespace_and_punct():
    raw = "HDFC  Mid-Cap   Fund.\n - Direct Plan"
    assert canonicalize(raw) == "HDFC MID CAP FUND"


def test_canonicalize_idempotent_on_clean_name():
    assert canonicalize("HDFC Defence Fund") == "HDFC DEFENCE FUND"


def test_canonicalize_empty_returns_empty():
    assert canonicalize("") == ""


# ---------------------------------------------------------------------------
# build_candidate_index — restricts to DIRECT+GROWTH, active, given AMC
# ---------------------------------------------------------------------------


def test_index_filters_by_amc_and_plan_option():
    sm = pl.DataFrame([
        _sm_row("A1", "HDFC Flexi Cap Fund - Direct Plan - Growth Option"),
        _sm_row("A2", "HDFC Flexi Cap Fund - Regular Plan - Growth Option", plan="REGULAR"),
        _sm_row("A3", "HDFC Flexi Cap Fund - Direct Plan - IDCW Option", option="IDCW"),
        _sm_row("B1", "ICICI Pru Bluechip Fund - Direct Plan - Growth", amc="icici_pru"),
    ])
    idx = build_candidate_index(sm, amc_slug="hdfc")
    # Only the DIRECT + GROWTH HDFC row should be in the index.
    assert len(idx) == 1
    assert "HDFC FLEXI CAP FUND" in idx
    assert idx["HDFC FLEXI CAP FUND"]["scheme_code"] == "A1"


def test_index_excludes_inactive_schemes():
    sm = pl.DataFrame([
        _sm_row("X1", "HDFC Equity Fund - Direct Plan - Growth", active=False),
    ])
    idx = build_candidate_index(sm, amc_slug="hdfc")
    assert idx == {}


def test_index_empty_for_unknown_amc():
    sm = pl.DataFrame([
        _sm_row("A1", "HDFC Flexi Cap Fund - Direct Plan - Growth"),
    ])
    idx = build_candidate_index(sm, amc_slug="unknown_amc")
    assert idx == {}


# ---------------------------------------------------------------------------
# match_one — fuzzy matching with threshold rejection
# ---------------------------------------------------------------------------


def test_match_exact_printed_name_finds_scheme():
    sm = pl.DataFrame([
        _sm_row("A1", "HDFC Flexi Cap Fund - Direct Plan - Growth Option"),
    ])
    idx = build_candidate_index(sm, "hdfc")
    r = match_one("HDFC Flexi Cap Fund", idx)
    assert r.matched_scheme_code == "A1"
    assert r.score >= 95


def test_match_near_miss_below_threshold_returns_none():
    """A printed name that doesn't resemble any scheme master entry must be
    rejected — silent mismatches would corrupt the managers table."""
    sm = pl.DataFrame([
        _sm_row("A1", "HDFC Flexi Cap Fund - Direct Plan - Growth"),
    ])
    idx = build_candidate_index(sm, "hdfc")
    r = match_one("Completely Unrelated Fund Name XYZ", idx)
    assert r.matched_scheme_code is None
    assert r.score < DEFAULT_THRESHOLD


def test_match_handles_real_world_printed_names():
    """AMC factsheets often print scheme names without plan/option suffix."""
    sm = pl.DataFrame([
        _sm_row("F1", "HDFC Flexi Cap Fund - Direct Plan - Growth Option"),
        _sm_row("F2", "HDFC Focused Fund - Direct Plan - Growth Option"),
        _sm_row("F3", "HDFC Multi-Asset Allocation Fund - Direct Plan - Growth Option"),
        _sm_row("F4", "HDFC Defence Fund - Direct Plan - Growth Option"),
    ])
    idx = build_candidate_index(sm, "hdfc")
    # Real printed strings from the April 2026 factsheet
    assert match_one("HDFC Flexi Cap Fund", idx).matched_scheme_code == "F1"
    assert match_one("HDFC Focused Fund", idx).matched_scheme_code == "F2"
    assert match_one("HDFC Multi-Asset Allocation Fund", idx).matched_scheme_code == "F3"
    assert match_one("HDFC Defence Fund", idx).matched_scheme_code == "F4"


def test_match_empty_input_returns_none():
    sm = pl.DataFrame([_sm_row("A1", "HDFC Flexi Cap Fund - Direct Plan - Growth")])
    idx = build_candidate_index(sm, "hdfc")
    assert match_one("", idx).matched_scheme_code is None


def test_match_empty_candidates_returns_none():
    r = match_one("HDFC Flexi Cap Fund", {})
    assert r.matched_scheme_code is None
