"""E4/E5/E6 — Markdown investor report (``rank/report.py``).

Synthetic run dirs in tmp_path; no DB anywhere. Covers: per-pick rendering
(ISIN, rank-of-N, liquidity sentence + SEVERE, drawdown lines), stage2
fallback note, missing-column tolerance ('—', never KeyError), the five
exception sections (each with present + degraded paths), the index-wins
marker, the static guidance facts, and glossary completeness over the
renderer's column list. ``young_rankable_filter`` is unit-tested as a pure
polars function.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from mfs.db.queries import young_rankable_filter
from mfs.rank import report as report_mod
from mfs.rank.report import (
    GLOSSARY,
    RENDERED_METRIC_COLUMNS,
    STAGE2_FALLBACK_NOTE,
    build_report,
)

AS_OF = date(2026, 6, 12)


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_csv(path)


def _pick_row(**overrides) -> dict:
    """A full stage-3 report row; tests override what they exercise.

    Deliberately omits ``max_dd_5y_pct`` / ``max_dd_5y_recovery_days`` /
    ``r_squared_3y`` / ``beta_3y`` — the spec's missing-COLUMN case."""
    row = {
        "stage2_rank": 1,
        "stage1_rank": 1,
        "as_of_date": AS_OF.isoformat(),
        "scheme_code": "100001",
        "canonical_category": "Flexi Cap",
        "benchmark_ticker": "NIFTY 500 TRI",
        "scheme_name": "Alpha Flexi Fund - Direct Plan - Growth",
        "isin_growth": "INF001A01001",
        "composite_score": 1.43,
        "ret_3y_median": 0.215,
        "ret_3y_p25": 0.195,
        "ret_5y_median": 0.180,
        "ret_5y_p25": 0.153,
        "alpha_3y_annualized": 0.089,
        "alpha_confidence": 0.85,
        "sortino_3y": 2.07,
        "info_ratio_3y": 2.30,
        "capture_up": 1.12,
        "capture_down": 0.95,
        "capture_efficiency": 1.18,
        "aum_crore": 45438.76,
        "aum_impact_cost_days": 924.0,
        "liquidity_flag": "SEVERE",
        "missing_disclosures": "",
        "partial_disclosure_flag": False,
        "style_drift_3y": 0.20,
        "active_share_median_1y": 0.62,
        "ptr_latest": 1.01,
        "max_dd_3y_pct": 18.4,
        "max_dd_3y_recovery_days": 210,
        "n_considered": 20,
        "n_survived": 5,
        "overlap_flag": False,
        "max_overlap_pct": None,
        "n_overlap_breaches": None,
        "overlap_breaches": "",
        "benchmark_fit_low_confidence": False,
        "benchmark_is_synthetic": False,
        "pick_minus_benchmark_3y": 0.021,
        "data_quality_flag": "GOOD",
    }
    row.update(overrides)
    return row


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    rd = tmp_path / AS_OF.isoformat()

    _write(rd / "stage3" / "mf_report.csv", [
        _pick_row(),
        _pick_row(
            stage2_rank=2, scheme_code="100002",
            scheme_name="Beta Flexi Fund - Direct Plan - Growth",
            isin_growth="INF002B01002",
            composite_score=0.91,
            aum_impact_cost_days=12.0, liquidity_flag="OK",
            missing_disclosures="active_share_median_1y;ptr_latest",
            partial_disclosure_flag=True,
            active_share_median_1y=None, ptr_latest=None,
            overlap_flag=True, max_overlap_pct=66.5, n_overlap_breaches=1,
            overlap_breaches="200001 Gamma Mid Fund (Mid Cap) @ 66.5%",
        ),
        _pick_row(
            stage2_rank=1, scheme_code="200001",
            canonical_category="Mid Cap",
            benchmark_ticker="NIFTY Midcap 150 TRI",
            scheme_name="Gamma Mid Fund - Direct Plan - Growth",
            isin_growth="INF003C01003",
            composite_score=0.55,
            # missing-CELL cases on top of the missing columns:
            alpha_3y_annualized=None, alpha_confidence=None,
            max_dd_3y_pct=None, max_dd_3y_recovery_days=None,
            aum_impact_cost_days=None, liquidity_flag=None,
            n_considered=8, n_survived=3,
            overlap_flag=True, max_overlap_pct=66.5, n_overlap_breaches=1,
            overlap_breaches="100002 Beta Flexi Fund (Flexi Cap) @ 66.5%",
            benchmark_fit_low_confidence=True, benchmark_is_synthetic=True,
        ),
    ])

    _write(rd / "stage2" / "coverage.csv", [
        {"canonical_category": "Flexi Cap", "n_considered": 20, "n_survived": 5},
        {"canonical_category": "Mid Cap", "n_considered": 8, "n_survived": 3},
    ])

    _write(rd / "stage1" / "excluded.csv", [
        {
            "scheme_code": "900001",
            "scheme_name": "Omega Stale Fund - Direct Plan - Growth",
            "canonical_category": "Flexi Cap",
            "data_quality_flag": "STALE",
            "exclusion_reason": "STALE_NAV",
            "missing_core_metrics": "",
        },
        {
            "scheme_code": "900002",
            "scheme_name": "Sigma Gap Fund - Direct Plan - Growth",
            "canonical_category": "Mid Cap",
            "data_quality_flag": "GOOD",
            "exclusion_reason": "MISSING_CORE_METRIC:alpha_3y_annualized",
            "missing_core_metrics": "alpha_3y_annualized",
        },
    ])

    _write(rd / "stage3" / "overlap_breaches.csv", [
        {
            "scheme_code_a": "100002",
            "scheme_name_a": "Beta Flexi Fund - Direct Plan - Growth",
            "category_a": "Flexi Cap",
            "stage2_rank_a": 2,
            "scheme_code_b": "200001",
            "scheme_name_b": "Gamma Mid Fund - Direct Plan - Growth",
            "category_b": "Mid Cap",
            "stage2_rank_b": 1,
            "overlap_pct": 66.5,
            "n_overlapping_securities": 23,
            "top_shared_holdings": "HDFC Bank Ltd (8.4%), ICICI Bank Ltd (7.1%)",
        },
    ])
    _write(rd / "stage3" / "overlap_pairs.csv", [
        {
            "scheme_code_a": "100002",
            "scheme_name_a": "Beta Flexi Fund - Direct Plan - Growth",
            "category_a": "Flexi Cap",
            "stage2_rank_a": 2,
            "scheme_code_b": "200001",
            "scheme_name_b": "Gamma Mid Fund - Direct Plan - Growth",
            "category_b": "Mid Cap",
            "stage2_rank_b": 1,
            "overlap_pct": 66.5,
            "n_overlapping_securities": 23,
            "top_shared_holdings": "HDFC Bank Ltd (8.4%), ICICI Bank Ltd (7.1%)",
            "reason": "",
        },
        {  # one side NOT in the final report — fallback must drop this pair
            "scheme_code_a": "100001",
            "scheme_name_a": "Alpha Flexi Fund - Direct Plan - Growth",
            "category_a": "Flexi Cap",
            "stage2_rank_a": 1,
            "scheme_code_b": "999999",
            "scheme_name_b": "Outsider Fund",
            "category_b": "Value",
            "stage2_rank_b": 4,
            "overlap_pct": 45.0,
            "n_overlapping_securities": 12,
            "top_shared_holdings": "Reliance Industries (5.0%)",
            "reason": "",
        },
    ])

    passive = rd / "passive_alternative.csv"
    table = pl.DataFrame([
        {
            "as_of_date": AS_OF.isoformat(),
            "canonical_category": "Flexi Cap",
            "benchmark_ticker": "NIFTY 500 TRI",
            "n_funds": 30,
            "benchmark_ret_3y_median": 0.155,
            "benchmark_ret_5y_median": 0.140,
            "fund_ret_3y_median": 0.182,
            "fund_ret_5y_median": 0.160,
            "median_fund_excess_3y": 0.027,
            "median_fund_excess_5y": 0.020,
            "index_wins": False,
            "benchmark_is_synthetic": False,
            "note": "",
        },
        {
            "as_of_date": AS_OF.isoformat(),
            "canonical_category": "Mid Cap",
            "benchmark_ticker": "NIFTY Midcap 150 TRI",
            "n_funds": 12,
            "benchmark_ret_3y_median": 0.210,
            "benchmark_ret_5y_median": 0.190,
            "fund_ret_3y_median": 0.190,
            "fund_ret_5y_median": 0.180,
            "median_fund_excess_3y": -0.020,
            "median_fund_excess_5y": -0.010,
            "index_wins": True,
            "benchmark_is_synthetic": False,
            "note": "",
        },
    ])
    passive.write_text("# passive_alternative caveat header line\n" + table.write_csv())

    return rd


def _build(rd: Path, young: pl.DataFrame | None = None) -> str:
    out = build_report(rd, as_of=AS_OF, young_funds=young)
    assert out == rd / "REPORT.md"
    return out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# E4 — core rendering
# ---------------------------------------------------------------------------

def test_report_created_with_isin_rank_liquidity_drawdown(run_dir: Path):
    text = _build(run_dir)
    assert (run_dir / "REPORT.md").is_file()
    # ISIN strings
    assert "INF001A01001" in text
    assert "INF003C01003" in text
    # 'rank 1 of N considered'
    assert "Rank 1 of 20 considered" in text
    assert "Rank 1 of 8 considered" in text
    # liquidity sentence + SEVERE flag for the 924-day row
    assert "exiting the least-liquid large positions would take ~924 trading days" in text
    assert "SEVERE" in text
    # drawdown lines
    assert "Worst historical fall" in text
    assert "fell 18.4% peak-to-trough" in text
    assert "recovered in ~210 days" in text
    # one '## ' section per category
    assert "## Flexi Cap" in text
    assert "## Mid Cap" in text


def test_stage2_fallback_note(run_dir: Path):
    (run_dir / "stage3" / "mf_report.csv").unlink()
    stage2_rows = pl.read_csv(
        run_dir / "stage2" / "coverage.csv"
    )  # only to prove stage2 dir exists; now write a stage2 report
    assert stage2_rows.height
    _write(run_dir / "stage2" / "mf_report.csv", [_pick_row()])
    text = _build(run_dir)
    assert STAGE2_FALLBACK_NOTE in text
    assert "Alpha Flexi Fund" in text


def test_missing_columns_and_cells_render_dash_without_raising(run_dir: Path):
    # max_dd_5y_*, r_squared_3y, beta_3y are absent COLUMNS for every row;
    # Gamma Mid Fund additionally has empty alpha/drawdown/liquidity CELLS.
    text = _build(run_dir)
    assert "—" in text
    assert "last 5y: —" in text  # missing column path
    # Gamma's alpha cell is empty → dash inside its bullet
    gamma = text.split("Gamma Mid Fund")[2]  # its pick section body
    assert "Edge over the benchmark (alpha): —" in gamma


def test_no_picks_at_all_still_writes_report(tmp_path: Path):
    rd = tmp_path / "empty-run"
    rd.mkdir()
    text = _build(rd)
    assert "No ranked picks were found" in text
    # static guidance still present
    assert "## Glossary" in text


# ---------------------------------------------------------------------------
# E5 — exception sections
# ---------------------------------------------------------------------------

def test_excluded_section_lists_fund_and_degrades(run_dir: Path):
    text = _build(run_dir)
    assert "## Excluded — insufficient data" in text
    assert "Omega Stale Fund" in text
    assert "STALE_NAV" in text
    assert "Sigma Gap Fund" in text

    (run_dir / "stage1" / "excluded.csv").unlink()
    text = _build(run_dir)
    assert "stage1/excluded.csv artifact not present in this run" in text


def test_partial_disclosure_column_path(run_dir: Path):
    text = _build(run_dir)
    assert "## Partial disclosure data" in text
    section = text.split("## Partial disclosure data")[1].split("## ")[0]
    assert "Beta Flexi Fund" in section
    assert "active share" in section
    assert "portfolio turnover" in section
    # fully-disclosed funds are NOT tagged
    assert "Alpha Flexi Fund" not in section


def test_partial_disclosure_fallback_from_nulls(run_dir: Path):
    # Strip the D3 columns: the fallback must derive the same tag from the
    # null ptr_latest / active_share_median_1y cells on Beta Flexi Fund.
    df = pl.read_csv(run_dir / "stage3" / "mf_report.csv", infer_schema_length=0)
    df.drop(["missing_disclosures", "partial_disclosure_flag"]).write_csv(
        run_dir / "stage3" / "mf_report.csv"
    )
    text = _build(run_dir)
    section = text.split("## Partial disclosure data")[1].split("## ")[0]
    assert "Beta Flexi Fund" in section
    assert "Alpha Flexi Fund" not in section


def test_overlap_breach_section_artifact_path(run_dir: Path):
    text = _build(run_dir)
    section = text.split("## Portfolio overlap warnings")[1].split("## ")[0]
    assert "66.5% overlap" in section
    assert "Beta Flexi Fund" in section and "Gamma Mid Fund" in section
    assert "HDFC Bank Ltd (8.4%)" in section
    # inline per-pick flag too
    assert "OVERLAP WARNING" in text


def test_overlap_breach_fallback_from_pairs(run_dir: Path):
    (run_dir / "stage3" / "overlap_breaches.csv").unlink()
    text = _build(run_dir)
    section = text.split("## Portfolio overlap warnings")[1].split("## ")[0]
    assert "derived from stage3/overlap_pairs.csv" in section
    assert "66.5% overlap" in section
    # the pair with a non-report scheme (999999) must be excluded
    assert "Outsider Fund" not in section

    (run_dir / "stage3" / "overlap_pairs.csv").unlink()
    text = _build(run_dir)
    assert "overlap_breaches.csv (and no derivable overlap_pairs.csv) not present" in text


def test_index_wins_marker_only_for_passive_category(run_dir: Path):
    text = _build(run_dir)
    flexi = text.split("## Flexi Cap")[1].split("## Mid Cap")[0]
    mid = text.split("## Mid Cap")[1].split("## Excluded")[0]
    assert "THE INDEX WON THIS CATEGORY" in mid
    assert "THE INDEX WON THIS CATEGORY" not in flexi
    # plain "consider the index" phrasing
    assert "Consider a low-cost index fund or ETF" in mid
    # the dedicated section lists Mid Cap only
    section = text.split("## Where the index fund wins")[1].split("## ")[0]
    assert "Mid Cap" in section
    assert "Flexi Cap" not in section

    (run_dir / "passive_alternative.csv").unlink()
    text = _build(run_dir)
    assert "passive_alternative.csv artifact not present in this run" in text


def test_benchmark_caveats_render_for_flagged_category(run_dir: Path):
    text = _build(run_dir)
    mid = text.split("## Mid Cap")[1].split("## Excluded")[0]
    flexi = text.split("## Flexi Cap")[1].split("## Mid Cap")[0]
    assert "Benchmark-fit caveat" in mid
    assert "Synthetic benchmark" in mid
    assert "Benchmark-fit caveat" not in flexi
    assert "Synthetic benchmark" not in flexi


def test_young_funds_grouped_by_category(run_dir: Path):
    young = pl.DataFrame([
        {
            "scheme_code": "300001",
            "scheme_name": "Newborn Mid Fund - Direct Plan - Growth",
            "canonical_category": "Mid Cap",
            "inception_date": date(2025, 1, 15),
            "isin_growth": "INF004D01004",
        },
        {
            "scheme_code": "300002",
            "scheme_name": "Toddler Flexi Fund - Direct Plan - Growth",
            "canonical_category": "Flexi Cap",
            "inception_date": date(2024, 7, 1),
            "isin_growth": None,
        },
    ])
    text = _build(run_dir, young=young)
    section = text.split("## Young funds — not yet eligible")[1].split("## ")[0]
    assert "2 fund(s)" in section
    assert "**Mid Cap** (1): Newborn Mid Fund" in section
    assert "**Flexi Cap** (1): Toddler Flexi Fund" in section
    assert "2025-01-15" in section
    assert "INF004D01004" in section
    # not-yet-eligible framing, not rejection
    assert "not yet eligible" in section

    text = _build(run_dir, young=None)
    assert "Young-fund eligibility data was not queried" in text


def test_young_rankable_filter_pure_function():
    as_of = date(2026, 6, 12)
    sm = pl.DataFrame(
        [
            # too old (inception before cutoff) → out
            ("OLD1", "Old Fund", "Mid Cap", date(2020, 1, 1), "amc::old", "DIRECT", "GROWTH", True),
            # young + rankable → IN
            ("YNG1", "Young Fund", "Mid Cap", date(2024, 1, 1), "amc::yng", "DIRECT", "GROWTH", True),
            # young but non-rankable category → out
            ("YNG2", "Debt Fund", "Liquid", date(2024, 1, 1), "amc::debt", "DIRECT", "GROWTH", True),
            # young but legacy bonus option → out
            ("YNG3", "Bonus Fund", "Mid Cap", date(2024, 1, 1), "amc::x_bonus", "DIRECT", "GROWTH", True),
            # young but REGULAR plan → out
            ("YNG4", "Regular Fund", "Mid Cap", date(2024, 1, 1), "amc::reg", "REGULAR", "GROWTH", True),
            # young but inactive → out
            ("YNG5", "Dead Fund", "Mid Cap", date(2024, 1, 1), "amc::dead", "DIRECT", "GROWTH", False),
            # null inception → out
            ("YNG6", "No-date Fund", "Mid Cap", None, "amc::nd", "DIRECT", "GROWTH", True),
            # exactly at the boundary (inception == cutoff) → out (strict >)
            ("YNG7", "Boundary Fund", "Mid Cap", date(2023, 6, 12), "amc::bnd", "DIRECT", "GROWTH", True),
            # null base_fund_id, young + rankable → IN
            ("YNG8", "Null-base Fund", "Flexi Cap", date(2025, 3, 1), None, "DIRECT", "GROWTH", True),
        ],
        schema={
            "scheme_code": pl.Utf8,
            "scheme_name": pl.Utf8,
            "canonical_category": pl.Utf8,
            "inception_date": pl.Date,
            "base_fund_id": pl.Utf8,
            "plan_type": pl.Utf8,
            "option_type": pl.Utf8,
            "is_active": pl.Boolean,
        },
        orient="row",
    )
    out = young_rankable_filter(sm, as_of, ["Mid Cap", "Flexi Cap"], min_years=3)
    assert sorted(out["scheme_code"].to_list()) == ["YNG1", "YNG8"]
    # sorted by category then inception
    assert out["canonical_category"].to_list() == ["Flexi Cap", "Mid Cap"]


# ---------------------------------------------------------------------------
# E6 — static guidance + glossary
# ---------------------------------------------------------------------------

def test_static_guidance_facts(run_dir: Path):
    text = _build(run_dir)
    # taxes / lock-ins / exit loads
    assert "3-year lock-in" in text
    assert "12.5%" in text
    assert "1.25" in text
    assert "20%" in text
    assert "1% if you redeem within" in text
    # hysteresis rule of thumb
    assert "2\nconsecutive runs" in text or "2 consecutive runs" in text.replace("\n", " ")
    assert "shortlist diff" in text
    # cadence
    assert "Quarterly is sufficient" in text
    # scope boundary
    assert "1,837 of 2,502" in text


def test_glossary_covers_every_rendered_metric_column(run_dir: Path):
    text = _build(run_dir)
    assert "## Glossary" in text
    glossary_section = text.split("## Glossary")[1]
    for col in RENDERED_METRIC_COLUMNS:
        assert col in GLOSSARY, f"GLOSSARY missing entry for {col}"
        assert f"`{col}`" in glossary_section, f"report glossary missing {col}"
    # the z-score convention entry
    assert "`z_* columns`" in glossary_section
