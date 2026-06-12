"""B7: the five monthly/quarterly freshness thresholds are live (non-null)
and check_freshness halts on whole-source staleness for each of them.

Config-sanity tests read configs/pipeline.yaml directly so a yaml regression
back to null can't hide behind FreshnessConfig's (matching) defaults; the
matrix tests drive check_freshness with monkeypatched MAX-date getters per
source and assert the raise/pass boundary around each threshold.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from mfs import freshness
from mfs.config import FreshnessConfig
from mfs.db import queries as q
from mfs.errors import FreshnessError

REPO_ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 6, 11)  # Thursday

# (yaml key, expected value) — the calibrated B7/D9 thresholds.
EXPECTED_THRESHOLDS = {
    "max_holdings_lag_days": 75,
    "max_constituents_lag_days": 75,  # D9 owns this one (derive cadence)
    "max_ptr_lag_days": 75,
    "max_stock_adv_lag_days": 10,
    "max_aum_lag_days": 150,
}


def _yaml_freshness() -> dict:
    with open(REPO_ROOT / "configs" / "pipeline.yaml") as f:
        return yaml.safe_load(f)["freshness"]


# --- config sanity -----------------------------------------------------------

def test_no_monthly_threshold_is_null_anymore():
    cfg = _yaml_freshness()
    for key in EXPECTED_THRESHOLDS:
        assert cfg[key] is not None, f"{key} regressed to null (gate disabled)"


def test_thresholds_match_calibrated_values():
    cfg = _yaml_freshness()
    for key, expected in EXPECTED_THRESHOLDS.items():
        assert cfg[key] == expected, f"{key}: yaml={cfg[key]} expected={expected}"


def test_freshness_config_defaults_mirror_yaml():
    """Drift guard: FreshnessConfig defaults must equal the yaml values, so a
    config-load failure can't silently loosen (or tighten) the gates."""
    cfg = _yaml_freshness()
    defaults = FreshnessConfig()
    for key in EXPECTED_THRESHOLDS:
        assert getattr(defaults, key) == cfg[key]


# --- check_freshness raise/pass matrix --------------------------------------

# (source label in the error, q getter name, fail_lag_days, pass_lag_days)
# pass lags are the empirically observed lags at calibration (2026-06-13),
# so these tests also pin "the gates do not insta-halt on today's data".
MATRIX = [
    ("Holdings", "latest_holdings_date", 100, 41),
    ("Index constituents", "latest_constituents_date", 100, 41),
    ("Portfolio turnover", "latest_ptr_date", 100, 41),
    ("Stock ADV", "latest_stock_adv_date", 20, 2),
    ("Scheme AUM", "latest_aum_date", 200, 104),
]
_GETTERS = [m[1] for m in MATRIX]


@pytest.fixture
def fresh_base(monkeypatch):
    """Make every non-matrix anchor (NAV/bench/rf/scheme-master) fresh and pin
    the config to the live FreshnessConfig defaults (== yaml values)."""
    monkeypatch.setattr(
        freshness, "_gather_latest",
        lambda: {
            "nav_latest": AS_OF,
            "rf_latest": AS_OF,
            "scheme_master_latest": AS_OF,
            "benchmarks": {"NIFTY 50 TRI": AS_OF},
        },
    )
    monkeypatch.setattr(
        freshness, "get_pipeline_config",
        lambda: type("P", (), {"freshness": FreshnessConfig()})(),
    )
    # Default: every matrix source fresh (1-day lag); tests override one.
    for getter in _GETTERS:
        monkeypatch.setattr(q, getter, lambda: AS_OF - timedelta(days=1))
    return monkeypatch


@pytest.mark.parametrize("label, getter, fail_lag, pass_lag", MATRIX)
def test_stale_source_raises_naming_the_source(
    fresh_base, label, getter, fail_lag, pass_lag
):
    fresh_base.setattr(q, getter, lambda: AS_OF - timedelta(days=fail_lag))

    with pytest.raises(FreshnessError) as excinfo:
        freshness.check_freshness(as_of=AS_OF, raise_on_fail=True)
    assert label in str(excinfo.value)
    assert "stale" in str(excinfo.value)


@pytest.mark.parametrize("label, getter, fail_lag, pass_lag", MATRIX)
def test_within_threshold_passes(fresh_base, label, getter, fail_lag, pass_lag):
    fresh_base.setattr(q, getter, lambda: AS_OF - timedelta(days=pass_lag))

    report = freshness.check_freshness(as_of=AS_OF, raise_on_fail=True)
    assert report.ok


def test_missing_source_with_threshold_set_fails(fresh_base):
    """A set threshold + empty table is a failure, not a silent skip."""
    fresh_base.setattr(q, "latest_holdings_date", lambda: None)

    with pytest.raises(FreshnessError, match="Holdings missing"):
        freshness.check_freshness(as_of=AS_OF, raise_on_fail=True)


def test_null_threshold_still_skips_the_check(fresh_base):
    """None remains the rollback switch: a null threshold disables only that
    source's check."""
    cfg = FreshnessConfig(max_holdings_lag_days=None)
    fresh_base.setattr(
        freshness, "get_pipeline_config",
        lambda: type("P", (), {"freshness": cfg})(),
    )
    fresh_base.setattr(q, "latest_holdings_date", lambda: AS_OF - timedelta(days=400))

    report = freshness.check_freshness(as_of=AS_OF, raise_on_fail=True)
    assert report.ok
