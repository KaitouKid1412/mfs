"""Tests for scheme_master category classification and the D1 diff-sync.

Focus on the closed-ended / interval guard: closed-ended tax-saver series must
NOT leak into the equity universe via the "ELSS"/"Tax Saver" substring rules.
The diff-sync suite (survivorship containment) covers present / absent /
re-appearing schemes and the mass-deactivation fail-fast guard.
"""

from __future__ import annotations

import contextlib
from datetime import date

import polars as pl
import pytest

from mfs.db import writers
from mfs.master.scheme_master import (
    _apply_single_option_default,
    _base_fund_id,
    _canonical_category,
    _classify_plan_option,
    _classify_sector,
    _direct_unknown_rankable,
    _strip_amc_name,
)


@pytest.mark.parametrize(
    "amfi_category",
    [
        "Close Ended Schemes(ELSS)",
        "Close Ended Schemes(Equity Scheme - ELSS)",
        "Closed Ended Schemes(ELSS)",
        "Interval Fund Schemes(Debt)",
        "Interval Fund Schemes(Equity)",
    ],
)
def test_closed_ended_and_interval_excluded(amfi_category):
    """Closed-ended / interval schemes resolve to None (dropped from universe)."""
    assert _canonical_category(amfi_category) is None


def test_open_ended_elss_still_classified():
    """Open-ended ELSS (the legitimate case) must still map to ELSS."""
    assert _canonical_category("Open Ended Schemes(ELSS)") == "ELSS"
    assert _canonical_category("Open Ended Schemes(Equity Scheme - ELSS)") == "ELSS"


def test_open_ended_equity_unaffected():
    assert _canonical_category("Open Ended Schemes(Large Cap Fund)") == "Large Cap"
    assert _canonical_category("Open Ended Schemes(Mid Cap Fund)") == "Mid Cap"
    assert _canonical_category("Open Ended Schemes(Flexi Cap Fund)") == "Flexi Cap"


def test_non_equity_returns_none():
    assert _canonical_category("Open Ended Schemes(Debt Scheme - Liquid Fund)") is None
    assert _canonical_category(None) is None
    assert _canonical_category("") is None


# ---------------------------------------------------------------------------
# Sector classification must key off the fund mandate, not the AMC house name.
# ---------------------------------------------------------------------------

def test_amc_name_does_not_drive_sector():
    """AMCs whose name contains a sector token must not mis-classify their own
    sector/thematic funds (regression: "BANK OF INDIA Manufacturing &
    Infrastructure" → Banking, "Bajaj Finserv Healthcare" → Banking)."""
    cases = [
        ("BANK OF INDIA Manufacturing & Infrastructure Fund", "Bank of India Mutual Fund", "Infrastructure"),
        ("Bank of India Consumption Fund", "Bank of India Mutual Fund", "Consumption"),
        ("Bank of India Business Cycle Fund", "Bank of India Mutual Fund", "Thematic"),
        ("BAJAJ FINSERV HEALTHCARE FUND", "Bajaj Finserv Mutual Fund", "Pharma & Healthcare"),
        ("BAJAJ FINSERV CONSUMPTION FUND", "Bajaj Finserv Mutual Fund", "Consumption"),
    ]
    for scheme, amc, expected in cases:
        assert _classify_sector(_strip_amc_name(scheme, amc)) == expected, scheme


def test_genuine_banking_fund_still_classifies():
    """Stripping the AMC name must NOT break a real banking fund — the mandate
    word survives."""
    assert _classify_sector(_strip_amc_name(
        "Bank of India Financial Services Fund", "Bank of India Mutual Fund"
    )) == "Banking & Financial Services"
    assert _classify_sector(_strip_amc_name(
        "SBI Banking & Financial Services Fund", "SBI Mutual Fund"
    )) == "Banking & Financial Services"


# ---------------------------------------------------------------------------
# Option parsing: 'Cumulative' → GROWTH, spelled-out / typo'd IDCW → IDCW,
# single-option default, DIRECT+UNKNOWN sanity listing.
# ---------------------------------------------------------------------------

# Real AMFI names of the 4 large ICICI Pru growth plans previously excluded
# from the universe (option_type stayed UNKNOWN).
ICICI_CUMULATIVE_NAMES = [
    "ICICI Prudential Equity Savings Fund - Direct Plan - Cumulative option",
    "ICICI Prudential Manufacturing Fund - Direct Plan - Cumulative Option",
    "ICICI Prudential India Opportunities Fund - Direct Plan - Cumulative Option",
    "ICICI Prudential Pharma Healthcare and Diagnostics (P.H.D) Fund"
    " - Direct Plan - Cumulative Option",
]


@pytest.mark.parametrize("name", ICICI_CUMULATIVE_NAMES)
def test_cumulative_option_is_growth(name):
    assert _classify_plan_option(name) == ("DIRECT", "GROWTH")


def test_cumulative_idcw_keeps_idcw_precedence():
    """'Cumulative IDCW' oddities must resolve to IDCW, not GROWTH."""
    assert _classify_plan_option("X Fund - Direct Plan - Cumulative IDCW") == (
        "DIRECT",
        "IDCW",
    )


@pytest.mark.parametrize(
    "name",
    [
        # Spelled-out IDCW phrase (real AMFI names)
        "Kotak Flexicap Fund - Payout of Income Distribution cum capital"
        " withdrawal option- Direct",
        "TATA Small Cap Fund Direct Plan - Reinvestment of Income Distribution"
        " cum capital withdrawal option",
        "360 ONE QUANT FUND DIRECT INCOME DISTRIBUTION CUM CAPITAL WITHDRAWAL",
        # Recurring AMFI typos
        "Canara Robeco Manufacturing Fund - Direct Plan - IDWC Option",
        "PGIM India Aggressive Hybrid Equity Fund-Direct Plan-Quarterly Divdend Option",
        "Baroda BNP Paribas Energy Opportunities Fund - Regular Plan - ICDW Option",
        # Abbreviated "Div" option token
        "UTI FTIF Series XXVII-VI (1113 Days) - Direct Plan - Annual Div Option",
    ],
)
def test_spelled_out_and_typo_idcw(name):
    assert _classify_plan_option(name)[1] == "IDCW"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # 'Dividend Yield' is the fund mandate, not an option token: growth
        # plans of the whole category were previously misclassified IDCW and
        # silently excluded from the universe. Real AMFI names.
        ("ICICI Prudential Dividend Yield Equity Fund Direct Plan Growth Option", "GROWTH"),
        ("UTI-Dividend Yield Fund.-Growth-Direct", "GROWTH"),
        ("HDFC Dividend Yield Fund - Growth Option Direct Plan", "GROWTH"),
        # ... while genuine IDCW plans of the same funds stay IDCW.
        ("HDFC Dividend Yield Fund - IDCW Option Direct Plan", "IDCW"),
        ("Tata Dividend Yield Fund-Direct Plan-IDCW Payout", "IDCW"),
        (
            "SBI Dividend Yield Fund - Direct Plan - Income Distribution cum"
            " Capital Withdrawal (IDCW) Option",
            "IDCW",
        ),
    ],
)
def test_dividend_yield_mandate_not_option_token(name, expected):
    assert _classify_plan_option(name)[1] == expected


def test_growth_and_bonus_unchanged():
    assert _classify_plan_option("Axis Bluechip Fund - Direct Plan - Growth") == (
        "DIRECT",
        "GROWTH",
    )
    # Legacy bonus-unit plans are neither growth nor IDCW: stay UNKNOWN.
    assert _classify_plan_option(
        "JM Aggressive Hybrid Fund (Direct) - Annual Bonus Option"
    ) == ("DIRECT", "UNKNOWN")


def _option_df(names: list[str], amc_slug: str = "samco") -> pl.DataFrame:
    """Build the minimal frame the post-pass operates on, via the real parsers."""
    rows = []
    for name in names:
        plan, option = _classify_plan_option(name)
        rows.append(
            {
                "scheme_name": name,
                "plan_type": plan,
                "option_type": option,
                "base_fund_id": _base_fund_id(name, amc_slug),
            }
        )
    return pl.DataFrame(rows)


def test_single_option_default_no_sibling_is_growth():
    """A token-less single-option fund (real Samco case) defaults to GROWTH."""
    df = _apply_single_option_default(
        _option_df(
            ["Samco Mid Cap Fund - Direct Plan", "Samco Mid Cap Fund - Regular Plan"]
        )
    )
    assert df["option_type"].to_list() == ["GROWTH", "GROWTH"]


def test_single_option_default_with_idcw_sibling_stays_unknown():
    """A resolved sibling in the same (base_fund_id, plan_type) group means
    genuine ambiguity — the token-less row must stay UNKNOWN."""
    df = _apply_single_option_default(
        _option_df(
            [
                "Samco Mid Cap Fund - Direct Plan",
                "Samco Mid Cap Fund - Direct Plan - IDCW",
            ]
        )
    )
    by_name = dict(df.select("scheme_name", "option_type").rows())
    assert by_name["Samco Mid Cap Fund - Direct Plan"] == "UNKNOWN"
    assert by_name["Samco Mid Cap Fund - Direct Plan - IDCW"] == "IDCW"


def test_single_option_default_skips_token_carrying_rows():
    """Rows that DO carry an option token (e.g. Bonus) never get the default,
    even with no resolved sibling."""
    df = _apply_single_option_default(
        _option_df(["PGIM India Large Cap Fund - Direct Plan - Bonus"], amc_slug="pgim")
    )
    assert df["option_type"].to_list() == ["UNKNOWN"]


def test_direct_unknown_rankable_listing():
    """Sanity listing: active DIRECT+UNKNOWN rows in rankable categories only."""
    df = pl.DataFrame(
        {
            "scheme_code": ["1", "2", "3", "4", "5"],
            "scheme_name": ["a", "b", "c", "d", "e"],
            "plan_type": ["DIRECT", "DIRECT", "REGULAR", "DIRECT", "DIRECT"],
            "option_type": ["UNKNOWN", "GROWTH", "UNKNOWN", "UNKNOWN", "UNKNOWN"],
            "canonical_category": ["Mid Cap", "Mid Cap", "Mid Cap", None, "Mid Cap"],
            "is_active": [True, True, True, True, False],
        }
    )
    assert _direct_unknown_rankable(df)["scheme_code"].to_list() == ["1"]


# ---------------------------------------------------------------------------
# D1 survivorship containment: diff-sync (writers.plan_scheme_master_sync /
# writers.sync_scheme_master). Departed schemes flip to is_active=false with a
# departed_at stamp; nothing is ever TRUNCATEd or DELETEd.
# ---------------------------------------------------------------------------


def test_plan_no_departures():
    assert writers.plan_scheme_master_sync({"1", "2"}, {"1", "2"}) == []


def test_plan_brand_new_scheme_is_not_a_departure():
    assert writers.plan_scheme_master_sync({"1", "2", "3"}, {"1", "2"}) == []


def test_plan_departed_codes_returned_sorted():
    out = writers.plan_scheme_master_sync(
        {"1", "2", "3", "4", "5", "6", "7", "8"},
        {"1", "2", "3", "4", "5", "6", "7", "8", "10", "9"},
    )
    assert out == ["10", "9"]


def test_plan_reappearing_inactive_code_not_in_plan():
    """A code that's in the DB but inactive (so not in active_codes) and back
    in today's snapshot must not be planned for deactivation."""
    assert writers.plan_scheme_master_sync({"1", "9"}, {"1"}) == []


def test_plan_exactly_at_limit_passes():
    # 1 of 5 active = exactly 20% — the guard is strictly greater-than.
    assert writers.plan_scheme_master_sync(
        {"1", "2", "3", "4"}, {"1", "2", "3", "4", "5"}
    ) == ["5"]


def test_plan_mass_deactivation_raises():
    # 2 of 5 active = 40% > 20% — a partial NAVAll page must halt the sync.
    with pytest.raises(RuntimeError, match="deactivate"):
        writers.plan_scheme_master_sync({"1", "2", "3"}, {"1", "2", "3", "4", "5"})


def test_plan_empty_db_first_sync_no_guard():
    assert writers.plan_scheme_master_sync({"1", "2"}, set()) == []


def _snapshot_df(codes: list[str]) -> pl.DataFrame:
    """Minimal frame with the 15 columns build() hands to the writer."""
    n = len(codes)
    return pl.DataFrame(
        {
            "scheme_code": codes,
            "isin_growth": pl.Series([None] * n, dtype=pl.Utf8),
            "isin_idcw": pl.Series([None] * n, dtype=pl.Utf8),
            "scheme_name": [f"Fund {c} - Direct Plan - Growth" for c in codes],
            "amc_name": ["Acme Mutual Fund"] * n,
            "amc_code": ["acme"] * n,
            "plan_type": ["DIRECT"] * n,
            "option_type": ["GROWTH"] * n,
            "amfi_category": ["Open Ended Schemes(Large Cap Fund)"] * n,
            "canonical_category": ["Large Cap"] * n,
            "benchmark_ticker": ["NIFTY 100 TRI"] * n,
            "inception_date": [date(2015, 1, 1)] * n,
            "base_fund_id": [f"acme::fund_{c}" for c in codes],
            "is_active": [True] * n,
            "last_seen_date": [date(2026, 6, 11)] * n,
        }
    )


class _FakeDB:
    """Captures every statement / upsert the sync writer issues, seeded with
    the scheme codes currently active in the fake scheme_master."""

    def __init__(self):
        self.active_codes: list[str] = []
        self.statements: list[tuple[str, tuple | None]] = []
        self.upserts: list[tuple[str, list[str], list[tuple]]] = []

    def updates(self) -> list[tuple[str, tuple | None]]:
        return [(s, p) for s, p in self.statements if "UPDATE scheme_master" in s]


class _FakeCursor:
    def __init__(self, db: _FakeDB):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self._db.statements.append((query, params))


class _FakeConn:
    def __init__(self, db: _FakeDB):
        self._db = db

    def execute(self, query, params=None):
        self._db.statements.append((query, params))
        rows = [(c,) for c in self._db.active_codes]

        class _Res:
            @staticmethod
            def fetchall():
                return rows

        return _Res()

    def cursor(self):
        return _FakeCursor(self._db)


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDB()

    @contextlib.contextmanager
    def _fake_connect(autocommit=False):
        yield _FakeConn(db)

    def _fake_copy_upsert(conn, table, columns, rows, pk):
        rows = list(rows)
        db.upserts.append((table, list(columns), rows))
        return len(rows)

    monkeypatch.setattr(writers, "connect", _fake_connect)
    monkeypatch.setattr(writers, "_copy_upsert", _fake_copy_upsert)
    return db


def test_sync_upserts_snapshot_rows_active_with_clear_departed_at(fake_db):
    """Snapshot rows (existing + brand-new) upsert with is_active=true and
    departed_at NULL; no scheme departed, so no deactivation UPDATE runs."""
    fake_db.active_codes = ["100"]
    n = writers.sync_scheme_master(_snapshot_df(["100", "200"]))
    assert n == 2
    table, cols, rows = fake_db.upserts[0]
    assert table == "scheme_master"
    i_active, i_dep = cols.index("is_active"), cols.index("departed_at")
    assert all(r[i_active] is True for r in rows)
    assert all(r[i_dep] is None for r in rows)
    assert fake_db.updates() == []


def test_sync_departed_scheme_kept_inactive_with_departed_at(fake_db):
    """A scheme absent from today's snapshot is KEPT — flipped inactive with
    departed_at=today via UPDATE; last_seen_date untouched; never DELETEd."""
    fake_db.active_codes = ["1", "2", "3", "4", "5", "6"]
    writers.sync_scheme_master(_snapshot_df(["1", "2", "3", "4", "5"]))
    updates = fake_db.updates()
    assert len(updates) == 1
    stmt, params = updates[0]
    assert "is_active = FALSE" in stmt
    assert "departed_at" in stmt
    assert "last_seen_date" not in stmt  # frozen, not rewritten
    assert params == (date.today(), ["6"])
    all_sql = " ".join(s for s, _ in fake_db.statements)
    assert "TRUNCATE" not in all_sql
    assert "DELETE" not in all_sql


def test_sync_reappearing_scheme_flips_back_active(fake_db):
    """A code present in the DB but inactive that re-appears in the snapshot
    is upserted with is_active=true / departed_at=NULL (snapshot wins) and is
    NOT in the deactivation set."""
    fake_db.active_codes = ["1"]  # '9' exists in the DB but is inactive
    writers.sync_scheme_master(_snapshot_df(["1", "9"]))
    _, cols, rows = fake_db.upserts[0]
    row9 = next(r for r in rows if r[cols.index("scheme_code")] == "9")
    assert row9[cols.index("is_active")] is True
    assert row9[cols.index("departed_at")] is None
    assert fake_db.updates() == []


def test_sync_mass_deactivation_halts_before_any_write(fake_db):
    """>20% of active rows missing (partial NAVAll page) raises BEFORE the
    upsert — neither the COPY-upsert nor the deactivation UPDATE may run."""
    fake_db.active_codes = ["1", "2", "3", "4"]
    with pytest.raises(RuntimeError, match="deactivate"):
        writers.sync_scheme_master(_snapshot_df(["1", "2"]))
    assert fake_db.upserts == []
    assert fake_db.updates() == []


# ---------------------------------------------------------------------------
# D5: benchmarks.csv loading + the 2026-06 Value/Energy remap
# ---------------------------------------------------------------------------


def test_benchmark_map_loads_with_comment_rows():
    """load_benchmark_map must skip '#' comment rows (D5 keeps a dated remap
    audit trail in configs/benchmarks.csv) and parse every mapping row."""
    from mfs.master.benchmark_map import load_benchmark_map

    load_benchmark_map.cache_clear()
    bm = load_benchmark_map()
    assert bm.columns[:2] == ["canonical_category", "benchmark_ticker"]
    assert not bm["canonical_category"].str.starts_with("#").any()
    assert bm.height >= 27  # one row per rankable category


def test_benchmark_map_d5_remap():
    """D5 (evidence: docs/audit/benchmark_refit_2026-06.md): Value regresses
    against NIFTY 500 TRI (refit median R-sq 0.897 vs 0.739 on the old factor
    index); Energy against NIFTY Infrastructure TRI (0.812 vs 0.648)."""
    from mfs.master.benchmark_map import benchmark_for, load_benchmark_map

    load_benchmark_map.cache_clear()
    assert benchmark_for("Value") == "NIFTY 500 TRI"
    assert benchmark_for("Energy") == "NIFTY Infrastructure TRI"


def test_benchmark_map_tickers_all_ingestable():
    """Every mapped ticker must be fetchable: either an NSE equity TRI in
    NSE_TRI_MAP or a synthesized hybrid — a remap to an unknown ticker would
    silently produce an empty benchmark series."""
    from mfs.ingest.benchmarks import NSE_TRI_MAP
    from mfs.ingest.synthetic_hybrid import SYNTHETIC_BENCHMARK_TICKERS
    from mfs.master.benchmark_map import load_benchmark_map

    load_benchmark_map.cache_clear()
    bm = load_benchmark_map()
    known = set(NSE_TRI_MAP) | set(SYNTHETIC_BENCHMARK_TICKERS)
    unknown = set(bm["benchmark_ticker"].to_list()) - known
    assert unknown == set()


def test_nifty_commodities_in_ingest_map():
    """D5: NIFTY Commodities TRI joins the NSE ingest map so the Energy refit
    can be re-run against it once the operator's network ingest lands."""
    from mfs.ingest.benchmarks import NSE_TRI_MAP

    assert NSE_TRI_MAP["NIFTY Commodities TRI"] == (
        "NIFTY COMMODITIES", "Nifty Commodities",
    )
