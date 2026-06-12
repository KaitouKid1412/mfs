"""D9: pipeline-integrated constituents derivation + active-share activation.

Covers the relocated derivation core (mfs.ingest.constituents.derive):
tracker auto-select with the index-size sanity band, dedupe/renormalize
helpers, the spec table (incl. the NIFTY 50 TRI / NIFTY Bank TRI tracker
mappings), derive_month end-to-end against monkeypatched DB seams, the
latest-month pipeline entry point, the extra-ticker ingestion discovery,
and the rank-side activation banner.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from mfs import paths
from mfs.db import queries as q
from mfs.errors import IngestError
from mfs.ingest.constituents import _run as crun
from mfs.ingest.constituents import derive
from mfs.rank import shortlist


# --- tracker auto-select: size-sanity band ----------------------------------

SPEC_50 = {"primary": "%nifty 50%", "excludes": [], "expected": 50}


def test_select_tracker_prefers_count_closest_to_expected(monkeypatch):
    monkeypatch.setattr(
        derive, "_candidate_trackers",
        lambda primary, excludes, ym: [
            ("B1", "Broad Index Fund", 264),   # out of band (>1.8x)
            ("E1", "Exact Tracker", 50),
            ("N1", "Near Tracker", 60),
        ],
    )
    assert derive._select_tracker(SPEC_50, "2026-05-01") == ("E1", "Exact Tracker")


def test_select_tracker_rejects_all_out_of_band(monkeypatch):
    monkeypatch.setattr(
        derive, "_candidate_trackers",
        lambda primary, excludes, ym: [("B1", "Broad", 264), ("S1", "Slim", 12)],
    )
    assert derive._select_tracker(SPEC_50, "2026-05-01") is None


def test_select_tracker_falls_back_to_alt_primary(monkeypatch):
    spec = dict(SPEC_50, alt_primary="%nifty index%")
    calls = []

    def fake_cands(primary, excludes, ym):
        calls.append(primary)
        return [] if primary == spec["primary"] else [("A1", "Alt Tracker", 50)]

    monkeypatch.setattr(derive, "_candidate_trackers", fake_cands)
    assert derive._select_tracker(spec, "2026-05-01") == ("A1", "Alt Tracker")
    assert calls == ["%nifty 50%", "%nifty index%"]


# --- dedupe / renormalize helpers --------------------------------------------

def test_dedupe_renormalize_sums_duplicates_and_renormalizes():
    rows = [
        {"isin": "INE1", "security_name": "A", "weight_pct": 30.0},
        {"isin": "INE1", "security_name": "A", "weight_pct": 10.0},
        {"isin": "INE2", "security_name": "B", "weight_pct": 10.0},
    ]
    out = derive._dedupe_renormalize(rows)
    assert [r["isin"] for r in out] == ["INE1", "INE2"]
    assert out[0]["weight_pct"] == 80.0
    assert out[1]["weight_pct"] == 20.0


def test_top_n_renormalizes_subset_to_100():
    rows = [{"isin": f"I{i}", "security_name": "x", "weight_pct": w}
            for i, w in enumerate([40.0, 30.0, 20.0, 10.0])]
    top = derive._top_n(rows, 2)
    assert len(top) == 2
    assert top[0]["weight_pct"] == pytest.approx(57.1429, abs=1e-3)
    assert sum(r["weight_pct"] for r in top) == pytest.approx(100.0, abs=1e-6)


# --- spec table: NIFTY 50 TRI + NIFTY Bank TRI mappings (REVIEW-EXTENDED) ----

def test_nifty_50_and_bank_specs_present_and_db_derived():
    assert "nifty_50_tri" in derive._SPECS
    assert derive._SPECS["nifty_50_tri"]["expected"] == 50
    assert "nifty_bank_tri" in derive._SPECS
    assert derive._SPECS["nifty_bank_tri"]["expected"] == 12
    # Both have Direct+Growth index-fund trackers in holdings_monthly, so they
    # derive from the DB — they must NOT be in the network ETF-fetch map.
    assert "nifty_50_tri" not in derive._ETF_TRACKERS
    assert "nifty_bank_tri" not in derive._ETF_TRACKERS
    # The old hidden helper is gone; hybrids sleeve off the real ticker now.
    assert "_nifty_50" not in derive._SPECS
    for hybrid in ("nifty_50_hybrid_50_50_tri", "nifty_50_hybrid_65_35_tri",
                   "nifty_equity_savings_tri"):
        assert derive._SPECS[hybrid]["sleeve_of"] == "nifty_50_tri"


def test_bank_spec_excludes_psu_and_private_variants():
    excl = derive._SPECS["nifty_bank_tri"]["excludes"]
    assert "%psu bank%" in excl and "%private bank%" in excl


# --- derive_month end-to-end (DB seams monkeypatched) -------------------------

RESOLVABLE = {"nifty_500_tri", "nifty_50_tri"}
HOLDING_ROWS = [
    {"isin": "INE1", "security_name": "A", "weight_pct": 50.0},
    {"isin": "INE2", "security_name": "B", "weight_pct": 30.0},
    {"isin": "INE3", "security_name": "C", "weight_pct": 20.0},
]


@pytest.fixture
def derivable(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(derive, "_derive_from_etf", lambda *a, **kw: [])
    monkeypatch.setattr(
        derive, "_select_tracker",
        lambda spec, ym: (("100", "Tracker X")
                          if spec is derive._SPECS.get("nifty_500_tri")
                          or spec is derive._SPECS.get("nifty_50_tri")
                          else None),
    )
    monkeypatch.setattr(
        derive, "_equity_holdings", lambda code, ym: [dict(r) for r in HOLDING_ROWS]
    )
    monkeypatch.setattr(
        paths, "index_constituents_manual_dir", lambda slug: tmp_path / slug
    )
    return tmp_path


def test_derive_month_writes_resolvable_and_derived_csvs(derivable):
    result = derive.derive_month("2026-05")

    status = {slug: st for slug, st, _n, _note in result["rows"]}
    assert status["nifty_500_tri"] == "OK"
    assert status["nifty_50_tri"] == "OK"
    assert status["nifty_100_tri"] == "OK"          # top-100 of nifty_500
    assert status["nifty_500_multicap_50_25_25_tri"] == "OK*"
    assert status["nifty_50_hybrid_65_35_tri"] == "OK*"   # NIFTY 50 sleeve
    assert status["nifty_bank_tri"] == "SKIPPED"          # no tracker wired here
    assert result["n_ok"] == 7  # 2 plain + 100 + multicap + 3 hybrid sleeves

    p = derivable / "nifty_50_tri" / "2026-05.csv"
    assert p.exists()
    with open(p, newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["isin"] for r in rows] == ["INE1", "INE2", "INE3"]
    assert sum(float(r["weight_pct"]) for r in rows) == pytest.approx(100.0, abs=1e-3)


def test_derive_month_dry_run_writes_nothing(derivable):
    result = derive.derive_month("2026-05", dry_run=True)
    assert result["n_ok"] == 7
    assert not any(derivable.iterdir())


def test_render_summary_lists_every_spec(derivable):
    result = derive.derive_month("2026-05", dry_run=True)
    text = derive.render_summary(result)
    assert "nifty_bank_tri" in text and "SKIPPED" in text
    assert f"Resolved {result['n_ok']}/{result['n_total']}" in text


# --- latest-month pipeline entry point ----------------------------------------

def test_latest_holdings_ym(monkeypatch):
    monkeypatch.setattr(q, "latest_holdings_date", lambda: date(2026, 5, 1))
    assert derive.latest_holdings_ym() == "2026-05"


def test_latest_holdings_ym_empty_table_raises(monkeypatch):
    monkeypatch.setattr(q, "latest_holdings_date", lambda: None)
    with pytest.raises(IngestError, match="holdings_monthly is empty"):
        derive.latest_holdings_ym()


def test_derive_latest_month_threads_ym(monkeypatch):
    seen = {}
    monkeypatch.setattr(q, "latest_holdings_date", lambda: date(2026, 5, 1))
    monkeypatch.setattr(
        derive, "derive_month",
        lambda ym, *, dry_run=False: seen.update(ym=ym, dry_run=dry_run) or {"ym": ym},
    )
    derive.derive_latest_month()
    assert seen == {"ym": "2026-05", "dry_run": False}


# --- ingestion discovery picks up the extra tickers ---------------------------

def test_discover_tickers_includes_extra_tickers_with_dirs(monkeypatch, tmp_path):
    base = tmp_path / "index_constituents" / "manual"
    (base / "nifty_50_tri").mkdir(parents=True)
    (base / "nifty_bank_tri").mkdir(parents=True)
    monkeypatch.setattr(paths, "raw_dir", lambda: tmp_path)

    found = crun.discover_tickers()
    assert "NIFTY 50 TRI" in found
    assert "NIFTY Bank TRI" in found


def test_discover_tickers_extra_tickers_need_a_dir(monkeypatch, tmp_path):
    (tmp_path / "index_constituents" / "manual").mkdir(parents=True)
    monkeypatch.setattr(paths, "raw_dir", lambda: tmp_path)
    assert crun.discover_tickers() == []


# --- activation banner (capsys) ------------------------------------------------

AS_OF = date(2026, 6, 13)


def _banner(capsys, matched, survivors=None):
    shortlist.emit_active_share_banner(
        AS_OF,
        survivors if survivors is not None else pl.DataFrame(),
        matched_months=matched,
    )
    return capsys.readouterr().out


def test_banner_one_of_three(capsys):
    out = _banner(capsys, [date(2026, 4, 1)])
    assert "active_share: n_matched_months=1/3" in out
    # Apr matched -> May + Jun still needed; Jun publishes ~2026-07-10.
    assert "estimated activation 2026-07-10" in out


def test_banner_two_of_three(capsys):
    out = _banner(capsys, [date(2026, 4, 1), date(2026, 5, 1)])
    assert "n_matched_months=2/3" in out
    assert "estimated activation 2026-07-10" in out


def test_banner_three_of_three_still_null(capsys):
    surv = pl.DataFrame({"scheme_code": ["1"], "active_share_median_1y": [None]})
    out = _banner(
        capsys, [date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)], surv
    )
    assert "n_matched_months=3/3" in out


def test_banner_zero_matched_months(capsys):
    out = _banner(capsys, [])
    assert "n_matched_months=0/3" in out
    # Nothing matched: 3 months after May (last completed) -> Aug, ~09-10.
    assert "estimated activation 2026-09-10" in out


def test_banner_goes_live_with_nonnull_share(capsys):
    surv = pl.DataFrame({
        "scheme_code": ["1", "2", "3", "4"],
        "active_share_median_1y": [62.5, None, 48.0, None],
    })
    out = _banner(capsys, [date(2026, 4, 1)], surv)
    assert "active_share LIVE: non-null for 2/4 stage-2 survivors (50%)" in out
    assert "estimated activation" not in out
