"""Pydantic plausibility bounds on ingest models (A1-1 + B14 schema half).

- NavDaily.nav: gt=0 (zero/negative NAV is a segregated/wound-up artifact;
  enforced at parse in ingest.amfi_nav, documented on the model).
- ParsedPtrRecord.ptr: gt=0, le=25 — ptr is a FRACTION; >25 means a percent
  value leaked through without /100 (the 100x unit flip).
- ParsedHoldingRecord.weight_pct: ge=-5, le=110 (shorts/derivatives margin
  and modest leverage allowed; beyond that it's a wrong-column parse).
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from mfs.schemas import NavDaily, ParsedHoldingRecord, ParsedPtrRecord


def _nav(nav: float) -> NavDaily:
    return NavDaily(scheme_code="100001", nav_date=date(2026, 6, 1), nav=nav)


def _ptr(ptr: float) -> ParsedPtrRecord:
    return ParsedPtrRecord(scheme_name_printed="Some Fund", ptr=ptr, source_amc="amc")


def _holding(weight_pct: float) -> ParsedHoldingRecord:
    return ParsedHoldingRecord(
        scheme_name_printed="Some Fund",
        security_name="Some Stock Ltd",
        weight_pct=weight_pct,
        instrument_type="EQUITY",
        source_amc="amc",
    )


class TestNavDailyBounds:
    def test_positive_nav_ok(self):
        assert _nav(450.1234).nav == 450.1234

    @pytest.mark.parametrize("bad", [0.0, -12.5])
    def test_nonpositive_nav_rejected(self, bad):
        with pytest.raises(ValidationError):
            _nav(bad)


class TestPtrBounds:
    @pytest.mark.parametrize("ok", [0.01, 0.0914, 1.27, 9.14, 25.0])
    def test_plausible_fraction_ok(self, ok):
        assert _ptr(ok).ptr == ok

    @pytest.mark.parametrize("bad", [0.0, -0.5])
    def test_nonpositive_rejected(self, bad):
        with pytest.raises(ValidationError):
            _ptr(bad)

    def test_percent_unit_flip_rejected(self):
        # 127% turnover written as 127 instead of 1.27 -> refuse at parse.
        with pytest.raises(ValidationError):
            _ptr(127.0)

    def test_just_above_cap_rejected(self):
        with pytest.raises(ValidationError):
            _ptr(25.01)


class TestHoldingWeightBounds:
    @pytest.mark.parametrize("ok", [-5.0, -1.2, 0.0, 9.85, 100.0, 110.0])
    def test_plausible_weights_ok(self, ok):
        assert _holding(ok).weight_pct == ok

    @pytest.mark.parametrize("bad", [-5.01, -50.0, 110.01, 985.0])
    def test_out_of_band_rejected(self, bad):
        with pytest.raises(ValidationError):
            _holding(bad)
