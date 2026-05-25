"""Upside / Downside capture ratios and Capture Ratio Efficiency.

UCR = mean(fund_ret | bench_ret > 0) / mean(bench_ret | bench_ret > 0)
DCR = mean(fund_ret | bench_ret < 0) / mean(bench_ret | bench_ret < 0)
CRE = UCR / DCR

Computed over rolling 3Y windows; reported as median across windows.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from mfs.compute.rolling import iter_windows


def _capture_one_window(fund: np.ndarray, bench: np.ndarray) -> tuple[float | None, float | None]:
    up_mask = bench > 0
    dn_mask = bench < 0
    if not up_mask.any() or not dn_mask.any():
        return None, None
    bench_up_mean = float(bench[up_mask].mean())
    bench_dn_mean = float(bench[dn_mask].mean())
    if bench_up_mean == 0 or bench_dn_mean == 0:
        return None, None
    fund_up_mean = float(fund[up_mask].mean())
    fund_dn_mean = float(fund[dn_mask].mean())
    ucr = fund_up_mean / bench_up_mean
    dcr = fund_dn_mean / bench_dn_mean
    return ucr, dcr


def rolling_capture(aligned: pl.DataFrame, window_years: int = 3, step: str = "1w") -> dict:
    if aligned.is_empty():
        return {"capture_up": None, "capture_down": None, "capture_efficiency": None}
    df = aligned.select(["date", "fund_log_ret", "bench_log_ret"]).drop_nulls()
    if df.is_empty():
        return {"capture_up": None, "capture_down": None, "capture_efficiency": None}
    pdf = df.to_pandas().set_index("date").sort_index()

    ucrs: list[float] = []
    dcrs: list[float] = []
    for window in iter_windows(pdf, window_years, step):
        fund = window["fund_log_ret"].values
        bench = window["bench_log_ret"].values
        ucr, dcr = _capture_one_window(fund, bench)
        if ucr is not None and dcr is not None:
            ucrs.append(ucr)
            dcrs.append(dcr)

    if not ucrs or not dcrs:
        return {"capture_up": None, "capture_down": None, "capture_efficiency": None}
    ucr_med = float(np.median(ucrs))
    dcr_med = float(np.median(dcrs))
    eff = ucr_med / dcr_med if dcr_med != 0 else None
    return {"capture_up": ucr_med, "capture_down": dcr_med, "capture_efficiency": eff}
