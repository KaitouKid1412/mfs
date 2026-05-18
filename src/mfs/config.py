from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

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


class FiltersConfig(BaseModel):
    plan_type: str = "DIRECT"
    option_type: str = "GROWTH"
    capture_efficiency_min: float = 1.15
    info_ratio_3y_min: float = 0.5
    r_squared_band: tuple[float, float] = (0.70, 0.90)


class OutputConfig(BaseModel):
    top_n_per_category: int = 10


class PipelineConfig(BaseModel):
    data_dir: str = "data"
    rolling: RollingConfig
    returns: ReturnsConfig
    ingest: IngestConfig
    quality: QualityConfig
    freshness: FreshnessConfig = Field(default_factory=FreshnessConfig)
    filters: FiltersConfig
    composite_weights: dict[str, float]
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
