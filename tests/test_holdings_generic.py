"""Unit tests for the generic SEBI-format holdings Excel parser.

Synthetic in-memory workbooks (no network, no cached fixtures) exercise the
column-position auto-detection and the percent-vs-fraction weight-unit
inference that let one parser serve the long tail of AMC layouts.
"""

from __future__ import annotations

import openpyxl
import pytest

from mfs.ingest.holdings._generic import classify_section, is_isin, parse_sebi_excel


def _write(tmp_path, rows, name="x.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    p = tmp_path / name
    wb.save(p)
    return p


def test_detects_columns_at_hdfc_offsets_percent(tmp_path):
    # HDFC-style: ISIN col B(1), name col D(3), weight col H(7), percent unit.
    rows = [
        ["Some Scheme", None, None, None, None, None, None, None],
        ["Portfolio as on 30-Apr-2026", None, None, None, None, None, None, None],
        [None, None, None, None, None, None, None, None],
        [None, None, None, None, None, None, None, None],
        [None, "ISIN", "Coupon", "Name Of the Instrument", "Industry",
         "Quantity", "Market Value", "% to NAV"],
        [None, "EQUITY & EQUITY RELATED", None, None, None, None, None, None],
        [None, "INE040A01034", None, "HDFC Bank Ltd", "Banks", 100, 5000, 9.24],
        [None, "INE002A01018", None, "Reliance Industries Ltd", "Petroleum",
         50, 4000, 7.10],
        [None, None, None, "Grand Total", None, None, None, 100.0],
    ]
    p = _write(tmp_path, rows)
    recs = list(parse_sebi_excel(p, "HDFC Test Fund", "hdfc"))
    by = {r.isin: r for r in recs}
    assert set(by) == {"INE040A01034", "INE002A01018"}
    assert by["INE040A01034"].weight_pct == pytest.approx(9.24)
    assert by["INE040A01034"].security_name == "HDFC Bank Ltd"
    assert by["INE040A01034"].instrument_type == "Equity"
    assert by["INE040A01034"].scheme_name_printed == "HDFC Test Fund"


def test_detects_fraction_unit_and_scales(tmp_path):
    # Nippon-style: weights stored as fractions (sum ~1.0) → scale ×100.
    rows = [
        ["Scheme banner", None, None, None],
        ["Monthly Portfolio Statement as on April 30,2026", None, None, None],
        ["ISIN", "Name of the Instrument", "Industry / Rating", "% to NAV"],
        ["Equity & Equity related", None, None, None],
        ["INE040A01034", "HDFC Bank Ltd", "Banks", 0.0924],
        ["INE002A01018", "Reliance Industries Ltd", "Petroleum", 0.0710],
        [None, "Grand Total", None, 1.0],
    ]
    p = _write(tmp_path, rows)
    by = {r.isin: r for r in parse_sebi_excel(p, "Nippon Test", "nippon")}
    assert by["INE040A01034"].weight_pct == pytest.approx(9.24, abs=1e-3)
    assert by["INE002A01018"].weight_pct == pytest.approx(7.10, abs=1e-3)


def test_weight_header_variants_aum(tmp_path):
    # '% to AUM' (SBI wording) must resolve to the weight column.
    rows = [
        [None, "Name of the Instrument", "ISIN", "Industry", "Qty",
         "Market value", "% to AUM"],
        [None, "Infosys Ltd", "INE009A01021", "IT", 10, 100, 12.5],
    ]
    p = _write(tmp_path, rows)
    by = {r.isin: r for r in parse_sebi_excel(p, "S", "x")}
    assert by["INE009A01021"].weight_pct == pytest.approx(12.5)


def test_no_header_yields_nothing(tmp_path):
    rows = [["just", "some", "text"], ["no", "isin", "here"]]
    p = _write(tmp_path, rows)
    assert list(parse_sebi_excel(p, "S", "x")) == []


def test_section_classification():
    assert classify_section("EQUITY & EQUITY RELATED") == "Equity"
    assert classify_section("Money Market Instruments") == "Debt"
    assert classify_section("REITs & InvITs") == "REIT/InvIT"
    assert classify_section("Net Current Assets") == "Cash"
    assert classify_section("Grand Total") is None
    assert classify_section("Subtotal") is None


def test_is_isin():
    assert is_isin("INE040A01034")
    assert not is_isin("EQUITY")
    assert not is_isin("INE040A0103")  # too short
