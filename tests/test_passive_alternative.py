"""D7 — passive-alternative table: index-wins verdict, golden excess values,
missing-5y nulls, the synthetic-benchmark note, and the mf_report
pick_minus_benchmark_3y join. All synthetic series, no DB."""

from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl

from mfs.rank import passive

AS_OF = date(2026, 6, 12)


def _const_growth_close(annual_ret: float, years: float, end: date = AS_OF) -> pl.DataFrame:
    """Daily close series growing at a constant calendar rate: every rolling
    window's calendar-CAGR equals ``annual_ret`` exactly, so the rolling
    median is ``annual_ret`` (golden value)."""
    n = int(365.25 * years)
    daily_log = math.log(1.0 + annual_ret) / 365.25
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    closes = [1000.0 * math.exp(daily_log * i) for i in range(n)]
    return pl.DataFrame({"date": dates, "close": closes})


def _metrics(cat: str, ticker: str, ret3: float, ret5: float | None,
             n: int = 10) -> pl.DataFrame:
    return pl.DataFrame({
        "canonical_category": [cat] * n,
        "benchmark_ticker": [ticker] * n,
        "ret_3y_median": [ret3] * n,
        "ret_5y_median": [ret5] * n,
    })


def _build(metrics: pl.DataFrame, series: dict[str, pl.DataFrame],
           cats: list[str]) -> pl.DataFrame:
    return passive.build_table(
        AS_OF,
        metrics=metrics,
        benchmark_series_fn=lambda t: series[t],
        step="1w",
        rankable_categories=cats,
    )


def test_index_wins_with_golden_excess():
    """Benchmark at 12%/yr vs median fund at 10%/yr -> index wins, excess
    values exact to 1e-9."""
    series = {"IDX TRI": _const_growth_close(0.12, 6.2)}
    metrics = _metrics("Cat A", "IDX TRI", ret3=0.10, ret5=0.11)
    table = _build(metrics, series, ["Cat A"])
    row = table.row(0, named=True)
    assert abs(row["benchmark_ret_3y_median"] - 0.12) < 1e-9
    assert abs(row["benchmark_ret_5y_median"] - 0.12) < 1e-9
    assert abs(row["median_fund_excess_3y"] - (-0.02)) < 1e-9
    assert abs(row["median_fund_excess_5y"] - (-0.01)) < 1e-9
    assert row["index_wins"] is True
    assert row["n_funds"] == 10
    assert row["benchmark_is_synthetic"] is False
    assert row["note"] == ""


def test_fund_beats_benchmark_index_does_not_win():
    series = {"IDX TRI": _const_growth_close(0.08, 6.2)}
    metrics = _metrics("Cat A", "IDX TRI", ret3=0.15, ret5=0.14)
    table = _build(metrics, series, ["Cat A"])
    row = table.row(0, named=True)
    assert row["index_wins"] is False
    assert abs(row["median_fund_excess_3y"] - 0.07) < 1e-9


def test_missing_5y_history_yields_nulls_without_crashing():
    """A benchmark with only ~3.5y of history has no 5y windows: 5y columns
    null, 3y verdict still produced."""
    series = {"YOUNG TRI": _const_growth_close(0.12, 3.5)}
    metrics = _metrics("Cat A", "YOUNG TRI", ret3=0.10, ret5=None)
    table = _build(metrics, series, ["Cat A"])
    row = table.row(0, named=True)
    assert row["benchmark_ret_5y_median"] is None
    assert row["median_fund_excess_5y"] is None
    assert abs(row["benchmark_ret_3y_median"] - 0.12) < 1e-9
    assert row["index_wins"] is True


def test_empty_benchmark_series_yields_null_verdict():
    series = {"EMPTY TRI": pl.DataFrame()}
    metrics = _metrics("Cat A", "EMPTY TRI", ret3=0.10, ret5=0.10)
    table = _build(metrics, series, ["Cat A"])
    row = table.row(0, named=True)
    assert row["benchmark_ret_3y_median"] is None
    assert row["index_wins"] is None


def test_synthetic_benchmark_carries_note():
    series = {"NIFTY 50 Hybrid 65:35 TRI": _const_growth_close(0.09, 6.2)}
    metrics = _metrics(
        "Aggressive Hybrid", "NIFTY 50 Hybrid 65:35 TRI", ret3=0.11, ret5=0.11,
    )
    table = _build(metrics, series, ["Aggressive Hybrid"])
    row = table.row(0, named=True)
    assert row["benchmark_is_synthetic"] is True
    assert row["note"] == passive.SYNTHETIC_NOTE


def test_non_rankable_categories_excluded():
    series = {"IDX TRI": _const_growth_close(0.12, 6.2)}
    metrics = pl.concat([
        _metrics("Cat A", "IDX TRI", 0.10, 0.10),
        _metrics("Unrankable", "IDX TRI", 0.10, 0.10),
    ])
    table = _build(metrics, series, ["Cat A"])
    assert table["canonical_category"].to_list() == ["Cat A"]


def test_attach_pick_excess_math():
    table = pl.DataFrame({
        "canonical_category": ["Cat A", "Cat B"],
        "benchmark_ret_3y_median": [0.12, 0.08],
    })
    survivors = pl.DataFrame({
        "scheme_code": ["S1", "S2", "S3"],
        "canonical_category": ["Cat A", "Cat B", "Cat C"],
        "ret_3y_median": [0.15, 0.07, 0.10],
    })
    out = passive.attach_pick_excess(survivors, table)
    d = dict(zip(out["scheme_code"], out["pick_minus_benchmark_3y"]))
    assert abs(d["S1"] - 0.03) < 1e-12
    assert abs(d["S2"] - (-0.01)) < 1e-12
    assert d["S3"] is None  # category absent from the table -> null, no crash


def test_write_csv_has_caveat_header_and_parses(tmp_path):
    series = {"IDX TRI": _const_growth_close(0.12, 6.2)}
    metrics = _metrics("Cat A", "IDX TRI", ret3=0.10, ret5=0.10)
    table = _build(metrics, series, ["Cat A"])
    path = passive.write_csv(table, tmp_path / "passive_alternative.csv")
    text = path.read_text()
    assert text.startswith("# passive_alternative (D7)")
    assert "survivor-biased" in text
    parsed = pl.read_csv(path, comment_prefix="#")
    assert parsed.height == 1
    assert parsed["index_wins"][0] is True
