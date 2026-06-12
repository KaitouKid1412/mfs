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


# ---------------------------------------------------------------------------
# match_one — collision resolution: unique best with margin; ambiguity → skip
# (B5: subset-name poaching, discriminator tokens, margin requirement)
# ---------------------------------------------------------------------------


def test_subset_name_against_superset_only_candidate_is_rejected():
    """'ABC Mid Cap Fund' with only the LARGE AND MID CAP sibling present
    token-set-scores 100 (subset tokens) but must NOT poach onto it."""
    sm = pl.DataFrame([
        _sm_row("L1", "ABC Large and Mid Cap Fund - Direct Plan - Growth", amc="abc"),
    ])
    idx = build_candidate_index(sm, "abc")
    r = match_one("ABC Mid Cap Fund", idx)
    assert r.matched_scheme_code is None
    assert r.ambiguous is True


def test_exact_canonical_equality_beats_superset_sibling():
    """With BOTH siblings present, the exact-equality fast path wins."""
    sm = pl.DataFrame([
        _sm_row("L1", "ABC Large and Mid Cap Fund - Direct Plan - Growth", amc="abc"),
        _sm_row("M1", "ABC Mid Cap Fund - Direct Plan - Growth", amc="abc"),
    ])
    idx = build_candidate_index(sm, "abc")
    r = match_one("ABC Mid Cap Fund", idx)
    assert r.matched_scheme_code == "M1"
    assert r.ambiguous is False


def test_motilal_multi_factor_fof_does_not_poach_multi_cap():
    """Regression for the confirmed April-2026 contamination: the Multi
    Factor Passive FoF's true target (154239) is absent from the
    DIRECT+GROWTH index, and its printed name scored 92.3 against the
    Multi Cap Fund — the FACTOR/PASSIVE discriminator tokens must veto it."""
    sm = pl.DataFrame([
        _sm_row(
            "152651",
            "Motilal Oswal Multi Cap Fund - Direct Plan - Growth",
            amc="motilal_oswal",
        ),
        _sm_row(
            "127042",
            "Motilal Oswal Midcap Fund - Direct Plan - Growth",
            amc="motilal_oswal",
        ),
    ])
    idx = build_candidate_index(sm, "motilal_oswal")
    r = match_one("Motilal Oswal Multi Factor Passive Fund of Funds", idx)
    assert r.matched_scheme_code is None
    assert r.ambiguous is True


def test_nifty_next_50_does_not_poach_nifty_50():
    """Plain ratio is too high (~90) for the subset rule here; the NEXT
    discriminator token is what must block the match."""
    sm = pl.DataFrame([
        _sm_row("N1", "ABC Nifty 50 Index Fund - Direct Plan - Growth", amc="abc"),
    ])
    idx = build_candidate_index(sm, "abc")
    r = match_one("ABC Nifty Next 50 Index Fund", idx)
    assert r.matched_scheme_code is None
    assert r.ambiguous is True


def test_nifty_next_50_matches_its_own_master_row():
    sm = pl.DataFrame([
        _sm_row("N1", "ABC Nifty 50 Index Fund - Direct Plan - Growth", amc="abc"),
        _sm_row("N2", "ABC Nifty Next 50 Index Fund - Direct Plan - Growth", amc="abc"),
    ])
    idx = build_candidate_index(sm, "abc")
    assert match_one("ABC Nifty Next 50 Index Fund", idx).matched_scheme_code == "N2"
    assert match_one("ABC Nifty 50 Index Fund", idx).matched_scheme_code == "N1"


def test_no_unique_best_within_margin_is_ambiguous():
    """Two superset siblings whose plain ratios differ by <5 points: the
    winner would hinge on noise, so the matcher must skip, not guess."""
    sm = pl.DataFrame([
        _sm_row("B1", "ABC Bluechip Equity Fund - Direct Plan - Growth", amc="abc"),
        _sm_row("B2", "ABC Bluechip Value Fund - Direct Plan - Growth", amc="abc"),
    ])
    idx = build_candidate_index(sm, "abc")
    r = match_one("ABC Bluechip Fund", idx)
    assert r.matched_scheme_code is None
    assert r.ambiguous is True


def test_ambiguous_rejection_logs_loud_warning():
    from structlog.testing import capture_logs

    sm = pl.DataFrame([
        _sm_row("L1", "ABC Large and Mid Cap Fund - Direct Plan - Growth", amc="abc"),
    ])
    idx = build_candidate_index(sm, "abc")
    with capture_logs() as logs:
        match_one("ABC Mid Cap Fund", idx)
    events = [e for e in logs if e["event"] == "scheme_match.ambiguous"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert events[0]["top_candidates"]  # candidates listed for diagnosis


def test_below_threshold_rejection_is_not_flagged_ambiguous():
    """A nothing-like-it name is a plain no-match, not an ambiguity."""
    sm = pl.DataFrame([
        _sm_row("A1", "HDFC Flexi Cap Fund - Direct Plan - Growth"),
    ])
    idx = build_candidate_index(sm, "hdfc")
    r = match_one("Completely Unrelated Fund Name XYZ", idx)
    assert r.matched_scheme_code is None
    assert r.ambiguous is False


# ---------------------------------------------------------------------------
# B5-extension: compound cap-token normalization, numeric-token guard,
# FOF <-> FUND OF FUNDS discriminator equivalence (Motilal heal regressions)
# ---------------------------------------------------------------------------


def test_compound_multicap_matches_spaced_multi_cap_not_midcap():
    """Live trace: printed 'Motilal Oswal Multicap Fund' scored 92.3 against
    the MIDCAP sibling because MULTICAP/MULTI+CAP are unrelated tokens to
    token_set_ratio. After compound normalization it must land EXACTLY on
    scheme 152651 (real scheme_master names)."""
    sm = pl.DataFrame([
        _sm_row(
            "152651",
            "Motilal Oswal Multi Cap Fund-Direct Plan Growth",
            amc="motilal_oswal",
        ),
        _sm_row(
            "127042",
            "Motilal Oswal Midcap Fund-Direct Plan-Growth Option",
            amc="motilal_oswal",
        ),
    ])
    idx = build_candidate_index(sm, "motilal_oswal")
    r = match_one("Motilal Oswal Multicap Fund", idx)
    assert r.matched_scheme_code == "152651"
    assert r.ambiguous is False
    assert r.score == 100.0  # exact canonical equality after normalization


def test_compound_normalization_applies_to_candidate_keys_too():
    """The compound split must hit candidate keys as well as printed names:
    a printed 'Mid Cap' (spaced) must exact-match an AMFI 'Midcap' row."""
    sm = pl.DataFrame([
        _sm_row(
            "127042",
            "Motilal Oswal Midcap Fund-Direct Plan-Growth Option",
            amc="motilal_oswal",
        ),
    ])
    idx = build_candidate_index(sm, "motilal_oswal")
    assert "MOTILAL OSWAL MID CAP FUND" in idx
    r = match_one("Motilal Oswal Mid Cap Fund", idx)
    assert r.matched_scheme_code == "127042"


def test_numeric_token_mismatch_rejects_nifty_500_onto_nifty_50():
    """Live trace: printed 'Motilal Oswal Nifty 500 Index Fund' matched the
    Nifty 50 fund at 98.5 (its own master row is option_type=UNKNOWN, so
    absent from the DIRECT+GROWTH index). {500} != {50} must reject the
    match as ambiguous regardless of score."""
    sm = pl.DataFrame([
        _sm_row(
            "147794",
            "Motilal Oswal Nifty 50 Index Fund - Direct plan - Growth",
            amc="motilal_oswal",
        ),
    ])
    idx = build_candidate_index(sm, "motilal_oswal")
    r = match_one("Motilal Oswal Nifty 500 Index Fund", idx)
    assert r.matched_scheme_code is None
    assert r.ambiguous is True


def test_numeric_token_mismatch_logs_loud_warning():
    from structlog.testing import capture_logs

    sm = pl.DataFrame([
        _sm_row(
            "147794",
            "Motilal Oswal Nifty 50 Index Fund - Direct plan - Growth",
            amc="motilal_oswal",
        ),
    ])
    idx = build_candidate_index(sm, "motilal_oswal")
    with capture_logs() as logs:
        match_one("Motilal Oswal Nifty 500 Index Fund", idx)
    events = [e for e in logs if e["event"] == "scheme_match.ambiguous"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert "numeric_tokens_mismatch" in events[0]["reason"]


def test_equal_numeric_token_sets_still_match():
    """{150, 50} on both sides is numeric-equal — the guard must not fire."""
    sm = pl.DataFrame([
        _sm_row(
            "K1",
            "Kotak Nifty Midcap 150 Momentum 50 Index Fund-Direct Plan-Growth",
            amc="kotak_mahindra",
        ),
    ])
    idx = build_candidate_index(sm, "kotak")
    r = match_one("Kotak Nifty Midcap 150 Momentum 50 Index Fund", idx)
    assert r.matched_scheme_code == "K1"
    assert r.ambiguous is False


def test_fof_printed_matches_fund_of_funds_master_row():
    """Live trace: printed '... Flexicap Passive FOF' was false-rejected
    against its own master row '... FLEXICAP PASSIVE FUND OF FUNDS DIRECT'
    (suffix '- Direct - Growth' leaves a trailing DIRECT) because the FOF
    discriminator comparison was literal. FOF <-> FUND OF FUNDS must be
    treated as equivalent (real scheme_master name, 154112)."""
    sm = pl.DataFrame([
        _sm_row(
            "154112",
            "Motilal Oswal Diversified Equity Flexicap Passive Fund of Funds- Direct - Growth",
            amc="motilal_oswal",
        ),
        _sm_row(
            "152651",
            "Motilal Oswal Multi Cap Fund-Direct Plan Growth",
            amc="motilal_oswal",
        ),
    ])
    idx = build_candidate_index(sm, "motilal_oswal")
    r = match_one("Motilal Oswal Diversified Equity Flexicap Passive FOF", idx)
    assert r.matched_scheme_code == "154112"
    assert r.ambiguous is False


def test_fof_discriminator_still_blocks_poach_onto_non_fof_sibling():
    """The FOF folding must only equate spellings, not weaken the
    discriminator: a printed FoF with no FoF master row must still be
    rejected against the active sibling."""
    sm = pl.DataFrame([
        _sm_row("G1", "ABC Gold Fund - Direct Plan - Growth", amc="abc"),
    ])
    idx = build_candidate_index(sm, "abc")
    r = match_one("ABC Gold Fund of Funds", idx)
    assert r.matched_scheme_code is None
    assert r.ambiguous is True


def test_normalization_keeps_existing_exact_match_intact():
    """Canonical-token normalization must not disturb a name that already
    matched exactly (no compound tokens, no glued digits)."""
    sm = pl.DataFrame([
        _sm_row("F1", "HDFC Flexi Cap Fund - Direct Plan - Growth Option"),
        _sm_row("F2", "HDFC Top 100 Fund - Direct Plan - Growth Option"),
    ])
    idx = build_candidate_index(sm, "hdfc")
    r = match_one("HDFC Flexi Cap Fund", idx)
    assert r.matched_scheme_code == "F1"
    assert r.score == 100.0
    # Numeric token present and equal on both sides — guard must not fire.
    assert match_one("HDFC Top 100 Fund", idx).matched_scheme_code == "F2"


def test_canonicalize_splits_compound_and_glued_digit_tokens():
    assert canonicalize("360 ONE FLEXICAP FUND") == "360 ONE FLEXI CAP FUND"
    assert (
        canonicalize("HDFC NIFTY500 Multicap 50:25:25 Index Fund")
        == "HDFC NIFTY 500 MULTI CAP 50 25 25 INDEX FUND"
    )
    assert (
        canonicalize("Edelweiss Nifty LargeMidcap250 Plus 8-13 Yr G-Sec 70:30 Index Fund")
        == "EDELWEISS NIFTY LARGE MID CAP 250 PLUS 8 13 YR G SEC 70 30 INDEX FUND"
    )
    # MIDSMALL inside MIDSMALLCAP must not double-split; standalone MIDSMALL splits.
    assert canonicalize("UTI Nifty Midsmallcap 400") == "UTI NIFTY MID SMALL CAP 400"
    assert (
        canonicalize("Motilal Oswal Nifty MidSmall Healthcare Index Fund")
        == "MOTILAL OSWAL NIFTY MID SMALL HEALTHCARE INDEX FUND"
    )
