"""Pinned end-to-end regression lock for ``shortlist.rank_deep`` (A2-9).

A deterministic synthetic universe (3 categories x 8 funds) flows through
the REAL Stage 1 → Stage 2 → Stage 3 code with only the DB readers/writers
and the output directory monkeypatched (no network, no database, no
today()-derived dates). Every ranking mechanism this workstream pinned is
exercised at least once:

  * hard-filter casualty            → ``FILTER:r_squared`` in excluded.csv
  * D2 core-metric gate             → ``MISSING_CORE_METRIC:alpha_3y_annualized``
  * stale fund                      → ``STALE_NAV``
  * missing-PTR fund (D3)           → kept, ``partial_disclosure_flag``,
                                      composite == disclosed twin − 0.026
  * overlap-breach pair (D4)        → BOTH kept in stage 3, ``overlap_flag``
                                      + overlap_breaches.csv pair row
  * liquidity SEVERE fund (A2-6/E3) → ``liquidity_flag == 'SEVERE'`` (>90d)
  * tied-composite pair (A2-11)     → scheme_code asc breaks the tie

Final per-category orderings, exclusion reasons, flag strings and Stage 2
composite scores are pinned EXACTLY (1e-9). Any future scoring change must
consciously update these pins.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from mfs import paths as paths_mod
from mfs.db import queries as q
from mfs.db import writers as w
from mfs.rank import shortlist

AS_OF = date(2026, 1, 31)

# Stage-3 survivor set (5 per category — pinned below).
FINAL_LARGE = ["100001", "100002", "100006", "100007", "100008"]
FINAL_MID = ["200001", "200002", "200003", "200004", "200005"]
FINAL_SMALL = ["300001", "300002", "300003", "300004", "300005"]
FINAL_CODES = FINAL_LARGE + FINAL_MID + FINAL_SMALL

# Frozen Stage 2 composite pins (computed once from configs/pipeline.yaml
# weights + penalties — 2026-06-13). Perturbing any stage-2 weight or penalty
# constant must fail these. Sanity anchors: 100006 == 100002 - 0.026 (D3
# missing-PTR penalty); 300001 == 300002 (A2-11 tie); every 12-day fund
# carries the same ~0.0095 log-ramp AUM penalty while 200003 (500d) carries
# the full 0.05.
EXPECTED_STAGE2_COMPOSITES: dict[str, float] = {
    "100001": 1.1029244920215595,
    "100002": 0.09162469832669612,
    "100006": 0.06562469832669612,
    "100007": -0.41402519852073555,
    "100008": -0.9196750953681673,
    "200001": 1.0621464814248502,
    "200002": 0.7559602635769531,
    "200003": 0.40927932677184614,
    "200004": 0.14358782788115854,
    "200005": -0.16259838996673887,
    "300001": 0.9091828107673857,
    "300002": 0.9091828107673857,
    "300003": 0.5322851320760315,
    "300004": 0.15538745338467713,
    "300005": -0.22151022530667708,
}


def _metrics_row(code: str, category: str, s: float, **over) -> dict:
    """One computed_metrics row. ``s`` is a monotone strength scalar — every
    weighted Phase-1 metric is affine in s, so within-category z-scores (and
    hence composite order) follow s exactly."""
    base = {
        "as_of_date": AS_OF,
        "scheme_code": code,
        "canonical_category": category,
        "benchmark_ticker": "NIFTY 100 TRI",
        "ret_3y_median": 0.10 + 0.02 * s,
        "ret_3y_p25": 0.06 + 0.02 * s,
        "ret_5y_median": 0.09 + 0.02 * s,
        "ret_5y_p25": 0.05 + 0.02 * s,
        "alpha_3y_annualized": 0.01 + 0.01 * s,
        "alpha_3y_tstat": 1.5,
        "alpha_confidence": 0.6,
        "sortino_3y": 0.8 + 0.1 * s,
        "info_ratio_3y": 0.3 + 0.1 * s,
        "capture_up": 1.0,
        "capture_down": 0.85,
        "capture_efficiency": 1.05 + 0.02 * s,
        "r_squared_3y": 0.85,
        "beta_3y": 1.0,
        "beta_3y_std": 0.05,
        "r_squared_3y_mean": 0.85,
        "style_drift_3y": 0.10,
        "active_share_median_1y": 0.50,
        "ptr_latest": 0.80,
        "aum_impact_cost_days": 12.0,
        "adv_unresolved_pct": 0.0,
        "data_quality_flag": "GOOD",
        "computed_at": datetime(2026, 1, 31, 12, 0, 0),
        "pipeline_version": "vtest",
    }
    base.update(over)
    return base


def _universe_rows() -> list[dict]:
    return [
        # --- Large Cap: the three exclusion mechanisms + the D3 PTR twin ---
        _metrics_row("100001", "Large Cap", 4),
        _metrics_row("100002", "Large Cap", 2),                       # L6's disclosed twin
        _metrics_row("100003", "Large Cap", 3, r_squared_3y=0.20),    # FILTER:r_squared
        _metrics_row("100004", "Large Cap", 3,
                     data_quality_flag="STALE"),                      # STALE_NAV
        _metrics_row("100005", "Large Cap", 3,
                     alpha_3y_annualized=None),                       # D2 core gate
        _metrics_row("100006", "Large Cap", 2, ptr_latest=None),      # D3: flagged, -0.026
        _metrics_row("100007", "Large Cap", 1),
        _metrics_row("100008", "Large Cap", 0),
        # --- Mid Cap: overlap-breach pair + liquidity SEVERE + final_size cut ---
        _metrics_row("200001", "Mid Cap", 7),                         # breaches w/ 200002
        _metrics_row("200002", "Mid Cap", 6),                         # breaches w/ 200001
        _metrics_row("200003", "Mid Cap", 5,
                     aum_impact_cost_days=500.0),                     # SEVERE (>90d)
        _metrics_row("200004", "Mid Cap", 4),
        _metrics_row("200005", "Mid Cap", 3),
        _metrics_row("200006", "Mid Cap", 2),                         # cut by final_size=5
        _metrics_row("200007", "Mid Cap", 1),
        _metrics_row("200008", "Mid Cap", 0),
        # --- Small Cap: A2-11 tied pair at the top ---
        _metrics_row("300001", "Small Cap", 5),                       # tie: code asc wins
        _metrics_row("300002", "Small Cap", 5),                       # identical twin
        _metrics_row("300003", "Small Cap", 4),
        _metrics_row("300004", "Small Cap", 3),
        _metrics_row("300005", "Small Cap", 2),
        _metrics_row("300006", "Small Cap", 1),
        _metrics_row("300007", "Small Cap", 0.5),
        _metrics_row("300008", "Small Cap", 0),
    ]


def _holdings_map() -> dict[str, list[tuple[str, float, str]]]:
    """(security_name, weight_pct, isin) per stage-3 survivor. Everyone holds
    a unique single stock (0% overlap) except 200001/200002, which share 60%."""
    hm = {c: [(f"Unique {c}", 100.0, f"INE{c}")] for c in FINAL_CODES}
    shared = [("Alpha Industries", 30.0, "INE0ALPHA"),
              ("Beta Industries", 30.0, "INE0BETA")]
    hm["200001"] = shared + [("M1 Unique", 40.0, "INE200001U")]
    hm["200002"] = shared + [("M2 Unique", 40.0, "INE200002U")]
    return hm


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Wire rank_deep's DB seams to the in-memory fixture universe and the
    output root to tmp_path. Returns (run, captured_history)."""
    metrics = pl.DataFrame(_universe_rows())
    master = pl.DataFrame([
        {
            "scheme_code": c,
            "scheme_name": f"Fund {c}",
            "base_fund_id": f"bf_{c}",
            "isin_growth": f"INF{c}GR",
        }
        for c in metrics["scheme_code"].to_list()
    ])
    hmap = _holdings_map()

    def _holdings(code: str, on_or_before=None) -> pl.DataFrame:
        rows = hmap.get(code, [])
        if not rows:
            return pl.DataFrame()
        return pl.DataFrame([
            {
                "scheme_code": code,
                "security_name": name,
                "as_of_month": date(2026, 1, 31),
                "weight_pct": wt,
                "isin": isin,
                "instrument_type": "Equity",
            }
            for name, wt, isin in rows
        ])

    def _no_db(*a, **kw):  # pragma: no cover - guard
        raise AssertionError("e2e test must never touch the database")

    captured: list[tuple[pl.DataFrame, date]] = []
    monkeypatch.setattr(q, "connect", _no_db)
    monkeypatch.setattr(w, "connect", _no_db)
    monkeypatch.setattr(q, "latest_computed_metrics_date", lambda: AS_OF)
    monkeypatch.setattr(q, "computed_metrics_at", lambda as_of: metrics)
    monkeypatch.setattr(
        q, "computed_metrics_for_schemes",
        lambda as_of, codes: metrics.filter(
            pl.col("scheme_code").is_in(list(codes))
        ),
    )
    monkeypatch.setattr(q, "scheme_master", lambda **kw: master)
    monkeypatch.setattr(
        q, "latest_scheme_aum",
        lambda code, on_or_before=None: (date(2025, 12, 31), 1000.0),
    )
    monkeypatch.setattr(q, "holdings_for_scheme", _holdings)
    monkeypatch.setattr(
        w, "persist_rank_history",
        lambda df, as_of: captured.append((df, as_of)) or df.height,
    )

    def run(name: str = "run") -> tuple[dict, Path]:
        base = tmp_path / name
        monkeypatch.setattr(paths_mod, "shortlist_dir", lambda s: base / s)
        result = shortlist.rank_deep(as_of=AS_OF, skip_phase2_compute=True)
        return result, base / AS_OF.isoformat()

    return run, captured


def _csv(path: Path) -> pl.DataFrame:
    return pl.read_csv(path)


def _codes(df: pl.DataFrame) -> list[str]:
    return [str(c) for c in df["scheme_code"].to_list()]


# ---------------------------------------------------------------------------
# The pinned end-to-end run
# ---------------------------------------------------------------------------


def test_rank_deep_e2e_pinned(harness):
    run, captured = harness
    result, out = run()

    # ---- result envelope -------------------------------------------------
    assert result["as_of"] == "2026-01-31"
    assert result["n_excluded"] == 3
    assert result["stage2"]["n_survivors"] == 15
    assert result["stage2"]["n_dropped"] == 0          # D3: never drops
    assert result["stage3"]["n_final"] == 15           # D4: keep-both
    assert result["stage3"]["n_flagged"] == 2
    assert result["stage3"]["n_breach_pairs"] == 1

    # ---- stage 1: excluded.csv — exact rows and reasons -------------------
    excluded = _csv(out / "stage1" / "excluded.csv")
    assert excluded.height == 3
    rows = {str(r["scheme_code"]): r for r in excluded.iter_rows(named=True)}
    assert list(rows) == ["100003", "100004", "100005"]  # (category, code) sort
    assert rows["100003"]["exclusion_reason"] == "FILTER:r_squared"
    assert rows["100004"]["exclusion_reason"] == "STALE_NAV"
    assert rows["100005"]["exclusion_reason"] == (
        "MISSING_CORE_METRIC:alpha_3y_annualized"
    )
    assert rows["100005"]["missing_core_metrics"] == "alpha_3y_annualized"
    assert rows["100005"]["scheme_name"] == "Fund 100005"

    # ---- stage 1: pinned per-category ordering ----------------------------
    assert _codes(_csv(out / "stage1" / "Large_Cap.csv")) == FINAL_LARGE
    assert _codes(_csv(out / "stage1" / "Mid_Cap.csv")) == [
        f"20000{i}" for i in range(1, 9)
    ]
    assert _codes(_csv(out / "stage1" / "Small_Cap.csv")) == [
        f"30000{i}" for i in range(1, 9)
    ]
    # Excluded funds appear NOWHERE downstream of excluded.csv.
    for f in ("Large_Cap", "Mid_Cap", "Small_Cap"):
        for stage in ("stage1", "stage2", "stage3"):
            df = _csv(out / stage / f"{f}.csv")
            assert not set(_codes(df)) & {"100003", "100004", "100005"}

    # ---- stage 2: pinned ordering, flags, composite pins ------------------
    lc = _csv(out / "stage2" / "Large_Cap.csv")
    mc = _csv(out / "stage2" / "Mid_Cap.csv")
    sc = _csv(out / "stage2" / "Small_Cap.csv")
    assert _codes(lc) == FINAL_LARGE
    assert _codes(mc) == FINAL_MID                      # final_size=5 cut
    assert _codes(sc) == FINAL_SMALL                    # 300001 before its twin

    # D3: the missing-PTR fund is flagged, kept, and sits exactly 0.026
    # below its fully-disclosed twin.
    by_code = {str(r["scheme_code"]): r for r in lc.iter_rows(named=True)}
    assert by_code["100006"]["partial_disclosure_flag"] is True
    assert by_code["100006"]["missing_disclosures"] == "ptr_latest"
    assert all(
        by_code[c]["partial_disclosure_flag"] is False
        for c in FINAL_LARGE if c != "100006"
    )
    delta = by_code["100002"]["composite_score"] - by_code["100006"]["composite_score"]
    assert abs(delta - 0.026) < 1e-9

    # A2-6/E3: liquidity tier strings.
    mc_by = {str(r["scheme_code"]): r for r in mc.iter_rows(named=True)}
    assert mc_by["200003"]["liquidity_flag"] == "SEVERE"
    assert all(
        mc_by[c]["liquidity_flag"] == "OK" for c in FINAL_MID if c != "200003"
    )

    # E1: ISIN + cohort-size context flow into the stage-2 outputs.
    # Large Cap: 8 funds minus 3 exclusions → pool of 5, all survive.
    assert by_code["100001"]["isin_growth"] == "INF100001GR"
    assert by_code["100001"]["n_considered"] == 5
    assert by_code["100001"]["n_survived"] == 5
    # Mid Cap: pool of 8, final_size=5 cut.
    assert mc_by["200001"]["n_considered"] == 8
    assert mc_by["200001"]["n_survived"] == 5

    # A2-11: the tied Small Cap twins really tie, and code asc orders them.
    sc_by = {str(r["scheme_code"]): r for r in sc.iter_rows(named=True)}
    assert abs(
        sc_by["300001"]["composite_score"] - sc_by["300002"]["composite_score"]
    ) < 1e-12
    assert (sc_by["300001"]["stage2_rank"], sc_by["300002"]["stage2_rank"]) == (1, 2)

    # Frozen absolute composite pins (1e-9) — the lock on the whole scoring
    # stack (weights, z-statistics, renormalization, every penalty).
    for df in (lc, mc, sc):
        for r in df.iter_rows(named=True):
            code = str(r["scheme_code"])
            assert abs(r["composite_score"] - EXPECTED_STAGE2_COMPOSITES[code]) < 1e-9, (
                code, r["composite_score"], EXPECTED_STAGE2_COMPOSITES[code],
            )

    # ---- stage 3: keep-both + flag ----------------------------------------
    s3_mc = _csv(out / "stage3" / "Mid_Cap.csv")
    assert _codes(s3_mc) == FINAL_MID                   # nothing dropped
    s3_by = {str(r["scheme_code"]): r for r in s3_mc.iter_rows(named=True)}
    assert s3_by["200001"]["overlap_flag"] is True
    assert s3_by["200002"]["overlap_flag"] is True
    assert s3_by["200001"]["n_overlap_breaches"] == 1
    assert s3_by["200001"]["overlap_breaches"] == (
        "200002 Fund 200002 (Mid Cap) @ 60.0%"
    )
    assert s3_by["200002"]["overlap_breaches"] == (
        "200001 Fund 200001 (Mid Cap) @ 60.0%"
    )
    assert s3_by["200001"]["max_overlap_pct"] == 60.0
    # E1 columns survive into stage 3 untouched.
    assert s3_by["200001"]["isin_growth"] == "INF200001GR"
    assert s3_by["200001"]["n_considered"] == 8
    assert all(
        s3_by[c]["overlap_flag"] is False for c in FINAL_MID
        if c not in ("200001", "200002")
    )
    assert _codes(_csv(out / "stage3" / "Large_Cap.csv")) == FINAL_LARGE
    assert _codes(_csv(out / "stage3" / "Small_Cap.csv")) == FINAL_SMALL

    # Breach + matrix artifacts.
    breaches = _csv(Path(result["stage3"]["breaches_file"]))
    assert breaches.height == 1
    rec = breaches.row(0, named=True)
    assert (str(rec["scheme_code_a"]), str(rec["scheme_code_b"])) == (
        "200001", "200002",
    )
    assert rec["overlap_pct"] == 60.0
    matrix = _csv(out / "stage3" / "overlap_matrix.csv")
    assert matrix.height == 15 * 14 // 2                # all pairs, unfiltered

    # ---- run history persisted in one batch, no live DB -------------------
    assert len(captured) == 1
    hist, hist_as_of = captured[0]
    assert hist_as_of == AS_OF
    assert set(hist["stage"].to_list()) == {1, 2, 3}
    assert set(
        hist.filter(~pl.col("included"))["exclusion_reason"].to_list()
    ) == {
        "FILTER:r_squared", "STALE_NAV",
        "MISSING_CORE_METRIC:alpha_3y_annualized",
    }
    # Stage 3 never excludes (D4).
    assert hist.filter(pl.col("stage") == 3).height == 15
    assert hist.filter((pl.col("stage") == 3) & ~pl.col("included")).height == 0


# ---------------------------------------------------------------------------
# A2-11 acceptance at the full-pipeline level
# ---------------------------------------------------------------------------


def test_rank_deep_byte_identical_across_runs(harness):
    """Re-running rank_deep on the same partition produces byte-identical
    CSVs in every stage directory (group_by hash order, dict iteration and
    pair orientation must not leak into the artifacts)."""
    run, _ = harness
    _, out_a = run("a")
    _, out_b = run("b")
    for stage in ("stage1", "stage2", "stage3"):
        a = {p.name: p.read_bytes() for p in sorted((out_a / stage).glob("*.csv"))}
        b = {p.name: p.read_bytes() for p in sorted((out_b / stage).glob("*.csv"))}
        assert set(a) == set(b)
        for name in a:
            assert a[name] == b[name], f"{stage}/{name} differs between runs"
