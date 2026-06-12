"""D6 — synthetic hybrid benchmarks: composition math, weights guard, and the
PATH B `benchmark_is_synthetic` disclosure column. No DB, no writes
(`upsert_benchmark_daily` is monkeypatched to capture)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl
import pytest

from mfs.ingest import synthetic_hybrid as sh
from mfs.rank.shortlist import with_benchmark_is_synthetic


def _patch_series(monkeypatch, captured: list[pl.DataFrame],
                  eq_daily_log: float = 0.0005, rate_daily: float = 0.0002,
                  n: int = 40):
    """Constant-growth NIFTY 50 + constant risk-free; capture the upsert."""
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(n)]
    closes = [100.0 * math.exp(eq_daily_log * i) for i in range(n)]
    nifty = pl.DataFrame({"date": dates, "close": closes})
    rf = pl.DataFrame({"date": dates, "rate_daily": [rate_daily] * n})

    monkeypatch.setattr(sh.q, "benchmark_series", lambda ticker: nifty)
    monkeypatch.setattr(sh.q, "risk_free_series", lambda: rf)
    monkeypatch.setattr(
        sh.w, "upsert_benchmark_daily", lambda df: captured.append(df)
    )
    return dates, closes, rate_daily


def test_synthesize_one_composition_exact(monkeypatch):
    """Synthesized close must compound w_eq*eq_logret + w_debt*rf_logret
    exactly (abs 1e-9), scaled to BASE_INDEX_VALUE."""
    captured: list[pl.DataFrame] = []
    n = 40
    eq_daily_log, rate_daily = 0.0005, 0.0002
    _patch_series(monkeypatch, captured, eq_daily_log, rate_daily, n)

    w_eq, w_debt = 0.65, 0.35
    rows = sh.synthesize_one("TEST 65:35", w_eq, w_debt)
    assert rows == n
    out = captured[0]
    assert out["ticker"].unique().to_list() == ["TEST 65:35"]
    assert out["is_synthetic"].all()

    rf_log = math.log(1.0 + rate_daily)
    cum = 0.0
    expected = []
    for i in range(n):
        eq_ret = 0.0 if i == 0 else eq_daily_log  # first diff is null->0
        cum += w_eq * eq_ret + w_debt * rf_log
        expected.append(sh.BASE_INDEX_VALUE * math.exp(cum))
    got = out["close"].to_list()
    assert len(got) == n
    for g, e in zip(got, expected):
        assert abs(g - e) < 1e-9


def test_synthesize_one_weights_must_sum_to_one(monkeypatch):
    captured: list[pl.DataFrame] = []
    _patch_series(monkeypatch, captured)
    with pytest.raises(ValueError, match="sum to 1.0"):
        sh.synthesize_one("BAD", 0.65, 0.30)
    assert captured == []  # guard fires before any write


def test_synthetic_ticker_set_matches_specs():
    assert sh.SYNTHETIC_BENCHMARK_TICKERS == {
        "NIFTY 50 Hybrid 65:35 TRI",
        "NIFTY 50 Hybrid 50:50 TRI",
        "NIFTY Equity Savings TRI",
    }


def test_mf_report_rows_carry_benchmark_is_synthetic():
    """PATH B: hybrid-category report rows are flagged true; equity rows
    false — the disclosure that hybrid alpha is overstated."""
    rows = pl.DataFrame({
        "scheme_code": ["H1", "H2", "H3", "E1"],
        "canonical_category": [
            "Aggressive Hybrid", "Balanced Advantage", "Equity Savings",
            "Large Cap",
        ],
        "benchmark_ticker": [
            "NIFTY 50 Hybrid 65:35 TRI", "NIFTY 50 Hybrid 50:50 TRI",
            "NIFTY Equity Savings TRI", "NIFTY 100 TRI",
        ],
    })
    out = with_benchmark_is_synthetic(rows)
    flags = dict(zip(out["scheme_code"], out["benchmark_is_synthetic"]))
    assert flags == {"H1": True, "H2": True, "H3": True, "E1": False}


def test_with_benchmark_is_synthetic_noop_without_ticker_column():
    df = pl.DataFrame({"scheme_code": ["X"]})
    assert "benchmark_is_synthetic" not in with_benchmark_is_synthetic(df).columns
