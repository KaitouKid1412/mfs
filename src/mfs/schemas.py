from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class NavDaily(BaseModel):
    """One (scheme_code, nav_date) NAV row.

    nav must be strictly positive: AMFI publishes 0.00000 for segregated /
    wound-up plans, and a zero NAV poisons log-return math downstream
    (log(0) = -inf). Enforced at parse time in ingest.amfi_nav and by the
    chk_nav_positive CHECK in schema.sql; this model documents the contract
    (it is not instantiated on the hot ingest path).
    """
    scheme_code: str
    nav_date: date
    nav: float = Field(gt=0)
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
    info_ratio_3y: Optional[float]
    capture_up: Optional[float]
    capture_down: Optional[float]
    capture_efficiency: Optional[float]
    r_squared_3y: Optional[float]
    beta_3y: Optional[float]
    # Phase 2 additive metrics. Nullable; populated incrementally across 2.0-2.3.
    beta_3y_std: Optional[float] = None
    r_squared_3y_mean: Optional[float] = None
    style_drift_3y: Optional[float] = None
    active_share_median_1y: Optional[float] = None
    ptr_latest: Optional[float] = None
    aum_impact_cost_days: Optional[float] = None
    data_quality_flag: str  # GOOD | POOR | INSUFFICIENT_HISTORY
    computed_at: datetime
    pipeline_version: str


class Holding(BaseModel):
    """One (scheme, security_name, as_of_month) holding row.

    AMC factsheets don't print ISINs in their portfolio listings, so the
    primary key is (scheme_code, security_name, as_of_month). ISIN is
    optional and gets backfilled at compute time by joining against
    index_constituents_monthly on normalized security_name.

    weight_pct is in percent (0-100). Bounds allow small negatives (shorts /
    derivatives margin) and modest >100 leverage: ge=-5, le=110. Anything
    outside is a parse/unit error, not a portfolio. instrument_type is the
    normalized section label from the AMC factsheet.

    NOTE: this model is currently declarative only — the holdings write path
    builds dicts/DataFrames directly and never instantiates it. Runtime
    enforcement happens on ParsedHoldingRecord (adapter output).
    """
    scheme_code: str
    security_name: str
    as_of_month: date
    weight_pct: float = Field(ge=-5, le=110)
    isin: Optional[str] = None
    instrument_type: str
    source_amc: str
    computed_at: datetime


class ParsedHoldingRecord(BaseModel):
    """Adapter output for one holding line before scheme_code matching.

    `security_name` is required; `isin` is optional and usually absent because
    factsheet portfolios don't print ISINs.

    weight_pct bounds (B14): ge=-5 (shorts / derivatives margin), le=110
    (modest leverage). A value outside this band means the adapter picked up
    a wrong column or a non-percent unit — refuse at parse rather than store.
    """
    scheme_name_printed: str
    security_name: str
    weight_pct: float = Field(ge=-5, le=110)
    isin: Optional[str] = None
    instrument_type: str
    source_amc: str


class IndexConstituent(BaseModel):
    """One (ticker, isin, as_of_month) row in index_constituents_monthly."""
    ticker: str
    isin: str
    as_of_month: date
    weight_pct: float
    security_name: Optional[str] = None


class PortfolioTurnover(BaseModel):
    """One (scheme, as_of_month) PTR row.

    ptr is stored as a fraction: a fund with 127% turnover has ptr=1.27.
    Plausibility bounds (B14): gt=0, le=25 (2500% annualized — beyond any
    real fund; a higher value means a percent-vs-fraction unit flip).

    NOTE: this model is currently declarative only — the PTR write path
    builds dicts/DataFrames directly and never instantiates it. Runtime
    enforcement happens on ParsedPtrRecord (adapter output).
    """
    scheme_code: str
    as_of_month: date
    ptr: float = Field(gt=0, le=25)
    source_amc: str
    computed_at: datetime


class ParsedPtrRecord(BaseModel):
    """Adapter output for one (scheme, ptr) pair before scheme_code matching.

    ptr is a FRACTION (127% turnover -> 1.27). Bounds (B14): gt=0, le=25 —
    a value above 25 (2500%) is a percent value leaking through without the
    /100 conversion; refuse at parse rather than store a 100x-wrong PTR.
    """
    scheme_name_printed: str
    ptr: float = Field(gt=0, le=25)
    source_amc: str
    # Optional period stamp. Monthly-factsheet adapters leave this None and the
    # orchestrator stamps the run's data month. Adapters that source PTR from a
    # less-frequent document (e.g. quant's abridged annual report) set it to the
    # document's true period-end so the value isn't mislabeled as the run month.
    as_of_month: Optional[date] = None


# Phase 2.3 ---------------------------------------------------------------


class StockAdvDaily(BaseModel):
    """One (isin, date) row from NSE daily bhavcopy."""
    isin: str
    symbol: Optional[str] = None
    date: date
    close: Optional[float] = None
    total_traded_value: float  # in INR


class SchemeAumMonthly(BaseModel):
    """One (scheme, month) AUM row in INR Crore."""
    scheme_code: str
    as_of_month: date
    aum_crore: float
    source_amc: str
    computed_at: datetime


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
    z_active_share_median_1y: Optional[float] = None
    z_style_drift_3y: Optional[float] = None
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
    beta_3y_std: Optional[float] = None
    r_squared_3y_mean: Optional[float] = None
    style_drift_3y: Optional[float] = None
    active_share_median_1y: Optional[float] = None
    ptr_latest: Optional[float] = None
    aum_impact_cost_days: Optional[float] = None
    data_quality_flag: str
