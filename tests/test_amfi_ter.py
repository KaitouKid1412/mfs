"""Tests for the AMFI TER ingester (C2) — pure parse / clean / match logic.

No network, no DB: the daily-disclosure rows and the scheme_master frame are
constructed in-test. The fixture mirrors the real
``/api/populate-te-rdata-revised`` row shape (base scheme name + per-plan TER
columns + a daily ``TER_Date``).
"""

from __future__ import annotations

from datetime import date

import polars as pl

from mfs.ingest.amfi_ter import (
    build_match_index,
    clean_ter_value,
    fy_labels_around,
    latest_complete_month_number,
    match_ter_to_schemes,
    month_number_to_as_of,
    parse_ter_rows,
)


def _row(name, ter_date, d_ter, **extra):
    """Minimal real-shaped daily TER row."""
    base = {
        "Scheme_Name": name,
        "TER_Date": f"{ter_date}T00:00:00.000Z",
        "D_TER": d_ter,
        "R_TER": "2.0000",
        "SchemeType_Desc": "Open Ended",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# clean_ter_value — the no-half-data band (0 < ter <= 3.0)
# ---------------------------------------------------------------------------


def test_clean_ter_value_accepts_in_band():
    assert clean_ter_value("0.5400") == 0.54
    assert clean_ter_value("1.33") == 1.33
    assert clean_ter_value(3.0) == 3.0
    assert clean_ter_value("0.0200") == 0.02


def test_clean_ter_value_rejects_out_of_band_and_garbage():
    for bad in ("0.0000", 0.0, -0.5, "3.5", 53.41, "abc", None, ""):
        assert clean_ter_value(bad) is None


# ---------------------------------------------------------------------------
# parse_ter_rows — dedup to latest TER_Date per scheme, clean direct TER
# ---------------------------------------------------------------------------


def test_parse_keeps_latest_date_and_drops_implausible():
    rows = [
        # Alpha: rate steps up over the month — keep the month-end (05-31) value.
        _row("Alpha Fund", "2026-05-01", "0.5400"),
        _row("Alpha Fund", "2026-05-15", "0.5400"),
        _row("Alpha Fund", "2026-05-31", "0.6000"),
        # Delta: a one-day blip on 05-31's prior day must not win over the
        # latest date.
        _row("Delta Fund", "2026-05-10", "1.2000"),
        _row("Delta Fund", "2026-05-31", "1.1000"),
        # Beta: undisclosed (0.0000) every day → rejected, never null-filled.
        _row("Beta Fund", "2026-05-15", "0.0000"),
        _row("Beta Fund", "2026-05-31", "0.0000"),
        # Gamma: implausible (> 3.0) → rejected.
        _row("Gamma Fund", "2026-05-31", "5.0000"),
    ]
    out = parse_ter_rows(rows)
    assert out == {"Alpha Fund": 0.60, "Delta Fund": 1.10}


def test_parse_empty():
    assert parse_ter_rows([]) == {}


# ---------------------------------------------------------------------------
# build_match_index — collision exclusion (fail-fast, never guess)
# ---------------------------------------------------------------------------


def _sm(rows):
    return pl.DataFrame(rows)


def _sm_row(code, name, plan="DIRECT", opt="GROWTH", active=True):
    return {
        "scheme_code": code,
        "scheme_name": name,
        "plan_type": plan,
        "option_type": opt,
        "is_active": active,
    }


def test_build_match_index_excludes_colliding_canonical_keys():
    sm = _sm([
        _sm_row("100", "Acme Flexi Cap Fund - Direct Plan - Growth"),
        # Two distinct codes canonicalize identically → colliding key dropped.
        _sm_row("200", "Beta Fund - Direct Plan - Growth"),
        _sm_row("201", "Beta Fund - Direct Plan - Growth"),
        # Regular/IDCW and inactive rows are filtered out before indexing.
        _sm_row("300", "Acme Flexi Cap Fund - Regular Plan - Growth", plan="REGULAR"),
        _sm_row("400", "Dead Fund - Direct Plan - Growth", active=False),
    ])
    idx = build_match_index(sm)
    assert "ACME FLEXI CAP FUND" in idx
    assert idx["ACME FLEXI CAP FUND"]["scheme_code"] == "100"
    # Colliding "BETA FUND" excluded entirely; filtered rows absent.
    assert "BETA FUND" not in idx
    assert "DEAD FUND" not in idx


# ---------------------------------------------------------------------------
# match_ter_to_schemes — resolve, skip ambiguous, drop code-conflicts
# ---------------------------------------------------------------------------


def test_match_resolves_unique_and_skips_unmatched():
    sm = _sm([_sm_row("100", "Acme Flexi Cap Fund - Direct Plan - Growth")])
    idx = build_match_index(sm)
    rows, stats = match_ter_to_schemes(
        {"Acme Flexi Cap Fund": 0.50, "Totally Unknown XYZ Fund": 0.70},
        idx, date(2026, 5, 1),
    )
    assert stats["matched"] == 1
    assert len(rows) == 1
    r = rows[0]
    assert r["scheme_code"] == "100"
    assert r["ter_direct_pct"] == 0.50
    assert r["as_of_month"] == date(2026, 5, 1)
    assert r["source"] == "amfi"


def test_match_drops_code_conflict():
    # Two distinct printed names both canonicalize onto the same fund with
    # DIFFERENT TERs → ambiguous output → that code is dropped, not guessed.
    sm = _sm([_sm_row("100", "Acme Flexi Cap Fund - Direct Plan - Growth")])
    idx = build_match_index(sm)
    rows, stats = match_ter_to_schemes(
        {"Acme Flexi Cap Fund": 0.50, "Acme Flexicap Fund": 0.80},
        idx, date(2026, 5, 1),
    )
    assert rows == []


# ---------------------------------------------------------------------------
# FY / month helpers
# ---------------------------------------------------------------------------


def test_fy_labels_around():
    assert fy_labels_around(date(2026, 6, 13)) == ["2025-2026", "2026-2027"]


def test_month_number_to_as_of():
    assert month_number_to_as_of("05-2026") == date(2026, 5, 1)


def test_latest_complete_month_excludes_current_month(monkeypatch):
    import mfs.ingest.amfi_ter as ter_mod

    # FY lists return June (current, partial) and May (complete) — May wins.
    def fake_list(fy):
        return {
            "2025-2026": [
                {"MonthYear": "May-2026", "MonthNumber": "05-2026"},
                {"MonthYear": "April-2026", "MonthNumber": "04-2026"},
            ],
            "2026-2027": [{"MonthYear": "June-2026", "MonthNumber": "06-2026"}],
        }.get(fy, [])

    monkeypatch.setattr(ter_mod, "fetch_month_list", fake_list)
    assert latest_complete_month_number(date(2026, 6, 13)) == "05-2026"
