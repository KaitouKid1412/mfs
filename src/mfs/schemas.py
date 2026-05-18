from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


class NavDaily(BaseModel):
    scheme_code: str
    nav_date: date
    nav: float
    isin_growth: Optional[str] = None
    isin_idcw: Optional[str] = None
    scheme_name: Optional[str] = None
    amc_name: Optional[str] = None
    amfi_category: Optional[str] = None


class BenchmarkDaily(BaseModel):
    ticker: str
    date: date
    close: float


class SchemeMasterRow(BaseModel):
    scheme_code: str
    isin_growth: Optional[str]
    isin_idcw: Optional[str]
    scheme_name: str
    amc_name: str
    amc_code: str
    plan_type: str  # DIRECT | REGULAR | UNKNOWN
    option_type: str  # GROWTH | IDCW | UNKNOWN
    amfi_category: Optional[str]
    canonical_category: Optional[str]
    benchmark_ticker: Optional[str]
    inception_date: Optional[date]
    base_fund_id: str
    is_active: bool
    last_seen_date: date


class ComputedMetrics(BaseModel):
    as_of_date: date
    scheme_code: str
    canonical_category: Optional[str]
    benchmark_ticker: Optional[str]
    ret_3y_median: Optional[float]
    ret_3y_p25: Optional[float]
    ret_5y_median: Optional[float]
    ret_5y_p25: Optional[float]
    alpha_3y_annualized: Optional[float]
    alpha_3y_tstat: Optional[float]
    sortino_3y: Optional[float]
    sortino_3y_peer_pct: Optional[float]
    info_ratio_3y: Optional[float]
    capture_up: Optional[float]
    capture_down: Optional[float]
    capture_efficiency: Optional[float]
    r_squared_3y: Optional[float]
    beta_3y: Optional[float]
    data_quality_flag: str  # GOOD | POOR | INSUFFICIENT_HISTORY
    computed_at: datetime
    pipeline_version: str


class ShortlistRow(BaseModel):
    as_of_date: date
    canonical_category: str
    rank: int
    scheme_code: str
    scheme_name: str
    composite_score: float
    z_ret_3y_median: Optional[float]
    z_ret_3y_p25: Optional[float]
    z_alpha_3y: Optional[float]
    z_sortino_3y: Optional[float]
    z_info_ratio_3y: Optional[float]
    z_capture_efficiency: Optional[float]
    ret_3y_median: Optional[float]
    ret_3y_p25: Optional[float]
    alpha_3y_annualized: Optional[float]
    sortino_3y: Optional[float]
    info_ratio_3y: Optional[float]
    capture_up: Optional[float]
    capture_down: Optional[float]
    capture_efficiency: Optional[float]
    r_squared_3y: Optional[float]
    beta_3y: Optional[float]
    data_quality_flag: str
