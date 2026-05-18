"""R² and β fall out of the alpha regression; this module is a thin re-export so the
compute layer stays cleanly organized."""

from __future__ import annotations

import polars as pl

from mfs.compute.alpha import rolling_alpha_beta_r2


def rolling_r2_beta(aligned: pl.DataFrame, window_years: int = 3, step: str = "1w") -> dict:
    out = rolling_alpha_beta_r2(aligned, window_years=window_years, step=step)
    return {"beta": out["beta"], "r2": out["r2"]}
