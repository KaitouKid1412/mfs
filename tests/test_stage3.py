"""Tests for Stage 3 — iterative pairwise-overlap drop.

Stage 3 takes the Stage 2 survivor frame and iteratively drops the
worse-Stage-2-ranked fund of the highest-overlap remaining pair until no
pair exceeds the threshold. Returns (final_picks, drops, overlap_pairs_log).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from mfs.rank.stage3 import apply_stage3, run


def _holding(name, weight, isin=None, instrument_type="Equity"):
    return {
        "security_name": name,
        "weight_pct": weight,
        "isin": isin,
        "instrument_type": instrument_type,
    }


def _survivor(scheme_code, name, category="Large Cap", stage2_rank=1):
    return {
        "scheme_code": scheme_code,
        "scheme_name": name,
        "canonical_category": category,
        "stage2_rank": stage2_rank,
    }


def _make_loader(holdings_by_code: dict[str, list[dict]]):
    def loader(scheme_code: str) -> pl.DataFrame:
        rows = holdings_by_code.get(scheme_code, [])
        return pl.DataFrame(rows) if rows else pl.DataFrame()
    return loader


# ---------------------------------------------------------------------------
# Empty / trivial cases
# ---------------------------------------------------------------------------


def test_no_drops_with_zero_survivors():
    final, drops, pairs = apply_stage3(pl.DataFrame(), _make_loader({}))
    assert final.is_empty()
    assert drops.is_empty()
    assert pairs.is_empty()


def test_no_drops_with_single_survivor():
    survivors = pl.DataFrame([_survivor("S1", "Fund 1")])
    final, drops, pairs = apply_stage3(
        survivors, _make_loader({"S1": [_holding("Stock A", 100.0, "INE_A")]}),
    )
    assert final.height == 1
    assert drops.is_empty()
    assert pairs.is_empty()


# ---------------------------------------------------------------------------
# Threshold filtering and dropping
# ---------------------------------------------------------------------------


def test_disjoint_portfolios_no_drops():
    """Two completely-disjoint funds → 0% overlap → both kept."""
    survivors = pl.DataFrame([
        _survivor("S1", "Fund 1", stage2_rank=1),
        _survivor("S2", "Fund 2", stage2_rank=2),
    ])
    loader = _make_loader({
        "S1": [_holding("A", 100.0, "INE_A")],
        "S2": [_holding("B", 100.0, "INE_B")],
    })
    final, drops, pairs = apply_stage3(survivors, loader, threshold_pct=30.0)
    assert final.height == 2
    assert drops.is_empty()


def test_identical_portfolios_kept_and_flagged():
    """Identical portfolios → 100% overlap → BOTH kept, pair flagged (D4)."""
    survivors = pl.DataFrame([
        _survivor("S_top", "Top", stage2_rank=1),
        _survivor("S_bot", "Bot", stage2_rank=2),
    ])
    holdings = [_holding("A", 50.0, "INE_A"), _holding("B", 50.0, "INE_B")]
    loader = _make_loader({"S_top": holdings, "S_bot": holdings})
    final, breaches, _ = apply_stage3(survivors, loader, threshold_pct=30.0)
    assert set(final["scheme_code"]) == {"S_top", "S_bot"}
    assert final.filter(pl.col("overlap_flag"))["scheme_code"].len() == 2
    assert breaches.height == 1
    rec = breaches.row(0, named=True)
    assert {rec["scheme_code_a"], rec["scheme_code_b"]} == {"S_top", "S_bot"}
    assert rec["overlap_pct"] == 100.0


def test_threshold_boundary_exactly_30_no_drop():
    """At exactly 30% overlap, no pair is dropped (strict >)."""
    survivors = pl.DataFrame([
        _survivor("S1", "Fund 1"),
        _survivor("S2", "Fund 2"),
    ])
    loader = _make_loader({
        "S1": [
            _holding("Shared", 30.0, "INE_SHARED"),
            _holding("Unique_A", 70.0, "INE_UA"),
        ],
        "S2": [
            _holding("Shared", 30.0, "INE_SHARED"),
            _holding("Unique_B", 70.0, "INE_UB"),
        ],
    })
    final, drops, _ = apply_stage3(survivors, loader, threshold_pct=30.0)
    assert final.height == 2
    assert drops.is_empty()


# ---------------------------------------------------------------------------
# Cascade drops
# ---------------------------------------------------------------------------


def test_three_overlapping_funds_all_kept_all_pairs_flagged():
    """3 identical portfolios → all 3 pairs breach → ALL kept, 3 pair flags
    (the old greedy cascade that left only the top fund is gone — D4)."""
    survivors = pl.DataFrame([
        _survivor("A", "Fund A", stage2_rank=1),
        _survivor("B", "Fund B", stage2_rank=2),
        _survivor("C", "Fund C", stage2_rank=3),
    ])
    holdings = [_holding("X", 100.0, "INE_X")]
    loader = _make_loader({"A": holdings, "B": holdings, "C": holdings})
    final, breaches, _ = apply_stage3(survivors, loader, threshold_pct=30.0)
    assert set(final["scheme_code"]) == {"A", "B", "C"}
    assert breaches.height == 3  # AB, AC, BC
    assert final.filter(pl.col("scheme_code") == "A").row(0, named=True)[
        "n_overlap_breaches"
    ] == 2


# ---------------------------------------------------------------------------
# Cross-category drops
# ---------------------------------------------------------------------------


def test_cross_category_breach_keeps_both():
    """Cross-category overlap flags the pair but never drops — a user who
    picked only Flexi Cap must still get its fund (D4)."""
    survivors = pl.DataFrame([
        _survivor("S_LC", "LC top", category="Large Cap", stage2_rank=1),
        _survivor("S_FC", "FC mid", category="Flexi Cap", stage2_rank=4),
    ])
    holdings = [_holding("A", 100.0, "INE_A")]
    loader = _make_loader({"S_LC": holdings, "S_FC": holdings})
    final, breaches, _ = apply_stage3(survivors, loader, threshold_pct=30.0)
    assert set(final["scheme_code"]) == {"S_LC", "S_FC"}
    assert breaches.height == 1
    fc = final.filter(pl.col("scheme_code") == "S_FC").row(0, named=True)
    assert fc["overlap_flag"] is True
    assert "S_LC" in fc["overlap_breaches"]


# ---------------------------------------------------------------------------
# Missing holdings handling
# ---------------------------------------------------------------------------


def test_pair_with_missing_holdings_reported_no_drop():
    """A survivor with empty holdings can't be compared — the pair lands in
    the overlap_pairs_log with reason='missing_holdings' and neither fund
    is dropped."""
    survivors = pl.DataFrame([
        _survivor("S_ok", "Has Holdings"),
        _survivor("S_empty", "No Holdings"),
    ])
    loader = _make_loader({
        "S_ok": [_holding("A", 100.0, "INE_A")],
        "S_empty": [],
    })
    final, drops, pairs = apply_stage3(survivors, loader, threshold_pct=30.0)
    assert final.height == 2
    assert drops.is_empty()
    assert pairs.height == 1
    row = pairs.row(0, named=True)
    assert row["overlap_pct"] is None
    assert row["reason"] == "missing_holdings"


# ---------------------------------------------------------------------------
# Top shared holdings string
# ---------------------------------------------------------------------------


def test_drops_carry_top_shared_holdings_string():
    """Drop row carries the 'Name (W%), ...' rendered cell."""
    survivors = pl.DataFrame([
        _survivor("S_top", "Top", stage2_rank=1),
        _survivor("S_bot", "Bot", stage2_rank=2),
    ])
    holdings = [
        _holding("HDFC Bank Ltd.", 40.0, "INE040A01034"),
        _holding("Reliance", 30.0, "INE002A01018"),
        _holding("Infosys", 30.0, "INE009A01021"),
    ]
    loader = _make_loader({"S_top": holdings, "S_bot": holdings})
    _, drops, _ = apply_stage3(survivors, loader, threshold_pct=30.0)
    cell = drops.row(0, named=True)["top_shared_holdings"]
    assert "HDFC Bank Ltd." in cell


# ---------------------------------------------------------------------------
# run() — file artifacts
# ---------------------------------------------------------------------------


def test_run_writes_files_even_when_no_drops(tmp_path):
    """Empty drop list → still writes header-only CSVs."""
    survivors = pl.DataFrame([
        _survivor("S1", "Fund 1"),
        _survivor("S2", "Fund 2"),
    ])
    loader = _make_loader({
        "S1": [_holding("A", 100.0, "INE_A")],
        "S2": [_holding("B", 100.0, "INE_B")],
    })
    result = run(survivors, loader, tmp_path)
    assert Path(result["breaches_file"]).exists()
    assert Path(result["overlap_pairs_file"]).exists()
    assert result["n_breach_pairs"] == 0
    assert result["n_final"] == 2


def test_run_keeps_both_and_flags_breach(tmp_path):
    """D4 keep-both: a 100% overlap pair is flagged, never dropped."""
    survivors = pl.DataFrame([
        _survivor("S_top", "Top", stage2_rank=1),
        _survivor("S_bot", "Bot", stage2_rank=2),
    ])
    holdings = [_holding("A", 100.0, "INE_A")]
    loader = _make_loader({"S_top": holdings, "S_bot": holdings})
    result = run(survivors, loader, tmp_path)
    assert result["n_final"] == 2          # both kept
    assert result["n_breach_pairs"] == 1
    breaches = pl.read_csv(result["breaches_file"])
    assert breaches.height == 1
    rec = breaches.row(0, named=True)
    assert {rec["scheme_code_a"], rec["scheme_code_b"]} == {"S_top", "S_bot"}


def test_run_writes_per_category_files(tmp_path):
    """Per-category CSVs land in stage3/ for the surviving funds."""
    survivors = pl.DataFrame([
        _survivor("S_LC_1", "LC1", category="Large Cap", stage2_rank=1),
        _survivor("S_FC_1", "FC1", category="Flexi Cap", stage2_rank=1),
    ])
    loader = _make_loader({
        "S_LC_1": [_holding("A", 100.0, "INE_A")],
        "S_FC_1": [_holding("B", 100.0, "INE_B")],
    })
    run(survivors, loader, tmp_path)
    assert (tmp_path / "stage3" / "Large_Cap.csv").exists()
    assert (tmp_path / "stage3" / "Flexi_Cap.csv").exists()


def test_run_handles_empty_survivors(tmp_path):
    result = run(pl.DataFrame(), _make_loader({}), tmp_path)
    assert Path(result["breaches_file"]).exists()
    assert Path(result["overlap_pairs_file"]).exists()
    assert result["n_final"] == 0
