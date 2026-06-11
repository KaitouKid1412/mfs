from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class RollingConfig(BaseModel):
    windows_years: list[int] = Field(default_factory=lambda: [3, 5])
    step: str = "1w"


class ReturnsConfig(BaseModel):
    trading_days_per_year: int = 252
    regression_basis: str = "log"
    display_basis: str = "simple"


class AmfiNavIngestConfig(BaseModel):
    backfill_start: date
    bulk_window_days: int = 90


class BenchmarksIngestConfig(BaseModel):
    history_start: date
    # Incremental ingest: per-ticker, fetch from MAX(date)-incremental_tail_days
    # forward instead of re-pulling the full ~13y history every run. The tail is
    # a generous overlap so NSE TRI restatements within the window self-correct
    # (upsert overwrites). A `--full` run ignores this and re-fetches everything.
    incremental_tail_days: int = 90


class IngestConfig(BaseModel):
    amfi_nav: AmfiNavIngestConfig
    benchmarks: BenchmarksIngestConfig


class QualityConfig(BaseModel):
    min_history_years_for_ranking: int = 5
    max_missing_pct_in_window: float = 0.05


class FreshnessConfig(BaseModel):
    """Maximum acceptable staleness for each curated dataset, checked before compute."""

    max_nav_lag_bdays: int = 5
    max_bench_lag_bdays: int = 5
    max_rf_lag_days: int = 14  # T-bills auction weekly; allow holiday slip
    max_scheme_master_lag_days: int = 1
    max_tbill_scrape_failure_rate: float = 0.05
    # Phase 2.1+ ingestion staleness windows. None = freshness check skipped
    # (used when an ingestion stage hasn't shipped yet, so the table is empty).
    max_holdings_lag_days: Optional[int] = 45        # monthly disclosures (AMFI mandate: by 10th)
    max_constituents_lag_days: Optional[int] = 45    # monthly index rebalance cadence
    max_ptr_lag_days: Optional[int] = 45             # same as holdings (same source PDF)
    # Phase 2.3
    max_stock_adv_lag_days: Optional[int] = 7        # NSE bhavcopy daily; allow weekend slip
    max_aum_lag_days: Optional[int] = 45             # AUM tracks holdings cadence


class FiltersConfig(BaseModel):
    plan_type: str = "DIRECT"
    option_type: str = "GROWTH"
    capture_efficiency_min: float = 1.15
    info_ratio_3y_min: float = 0.5
    r_squared_band: tuple[float, float] = (0.70, 0.90)


class OutputConfig(BaseModel):
    top_n_per_category: int = 10


class PtrPenalty(BaseModel):
    """Soft-penalty curve for high Portfolio Turnover Ratio (Phase 2.2.H).

    PTR is stored as a fraction (1.27 = 127% turnover). The penalty ramps
    linearly above `threshold` and caps at `max_penalty`:

        penalty(ptr) = clip(0, max_penalty,
                            max_penalty × max(0, (ptr − threshold) / threshold))

    For threshold=1.50 and max_penalty=0.10: PTR=1.50 → 0, PTR=3.00 → 0.10
    (capped). Disabled flag → no deduction even when PTR is high.
    """
    enabled: bool = True
    max_penalty: float = 0.10
    threshold: float = 1.50   # 150% PTR begins the penalty ramp


class AumImpactCostPenalty(BaseModel):
    """Soft-penalty curve for AUM Impact Cost (days-to-liquidate).

    Mirrors PtrPenalty but on a days scale: the ramp begins at
    ``threshold_days`` and caps at ``max_penalty``. Null impact-cost → exempt
    (the row didn't reach Stage 2 anyway if Phase 2.3.C couldn't measure it).
    """
    enabled: bool = True
    max_penalty: float = 0.05
    threshold_days: float = 5.0


class SoftPenaltiesConfig(BaseModel):
    """Non-linear penalty terms layered on top of the Stage 2 z-score
    composite. Each penalty has its own enabled flag so we can A/B individual
    signals without re-jiggering the weights vector. Stage 1 ignores these.
    """
    ptr: PtrPenalty = Field(default_factory=PtrPenalty)
    aum_impact_cost: AumImpactCostPenalty = Field(default_factory=AumImpactCostPenalty)


class PipelineConfig(BaseModel):
    data_dir: str = "data"
    rolling: RollingConfig
    returns: ReturnsConfig
    ingest: IngestConfig
    quality: QualityConfig
    freshness: FreshnessConfig = Field(default_factory=FreshnessConfig)
    filters: FiltersConfig
    composite_weights_stage1: dict[str, float]
    composite_weights_stage2: dict[str, float]
    soft_penalties: SoftPenaltiesConfig = Field(default_factory=SoftPenaltiesConfig)
    output: OutputConfig
    pipeline_version: str = "v1.0.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MFS_", env_file=".env", extra="ignore")

    data_dir: Path = REPO_ROOT / "data"
    user_agent: str = "mfs-pipeline/0.1 (+local research)"
    http_timeout: int = 60
    config_path: Path = REPO_ROOT / "configs" / "pipeline.yaml"
    benchmarks_csv: Path = REPO_ROOT / "configs" / "benchmarks.csv"
    thresholds_yaml: Path = REPO_ROOT / "configs" / "category_thresholds.yaml"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_pipeline_config() -> PipelineConfig:
    settings = get_settings()
    with open(settings.config_path) as f:
        raw = yaml.safe_load(f)
    return PipelineConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_thresholds() -> dict:
    settings = get_settings()
    with open(settings.thresholds_yaml) as f:
        return yaml.safe_load(f)
