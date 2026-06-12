"""Tests for D2 point-in-time per-run snapshots (scheme_master_history +
rank_history).

Covers, without a live DB:
  * the filters reason-frame — excluded funds get the FIRST failing filter in
    application order, using the contract vocabulary
    (INSUFFICIENT_HISTORY | MISSING_CORE_METRIC:<name> | STALE_NAV | FILTER:<name>);
  * build_rank_history shape — stage 1/2/3 rows, included flags, ranks, reasons;
  * the writers (fake connect/_copy_upsert, the test_scheme_master pattern) —
    DELETE + insert in ONE transaction, replace-on-rerun semantics;
  * both persist hooks — scheme_master.build() snapshots post-sync, rank_deep
    persists one batch at its tail;
  * the queries readers on a fake connection;
  * the three rank_history column registries (shortlist / writers / queries)
    plus schema.sql stay identical.
"""

from __future__ import annotations

import contextlib
import re
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from mfs.db import queries as q
from mfs.db import writers
from mfs.rank import shortlist
from mfs.rank.filters import apply_hard_filters

AS_OF = date(2026, 6, 8)


def _row(scheme, cat="Large Cap", **overrides):
    base = {
        "as_of_date": AS_OF,
        "scheme_code": scheme,
        "canonical_category": cat,
        "benchmark_ticker": "NIFTY 100 TRI",
        "ret_3y_median": 0.18,
        "ret_3y_p25": 0.12,
        "ret_5y_median": 0.16,
        "ret_5y_p25": 0.10,
        "alpha_3y_annualized": 0.03,
        "alpha_3y_tstat": 1.8,
        "sortino_3y": 1.2,
        "info_ratio_3y": 0.7,
        "capture_up": 1.05,
        "capture_down": 0.85,
        "capture_efficiency": 1.235,
        "r_squared_3y": 0.82,
        "beta_3y": 0.95,
        "beta_3y_std": None,
        "r_squared_3y_mean": None,
        "style_drift_3y": 0.1,
        "active_share_median_1y": None,
        "ptr_latest": 0.5,
        "aum_impact_cost_days": 1.0,
        "data_quality_flag": "GOOD",
        "computed_at": datetime.utcnow(),
        "pipeline_version": "v1.0.0",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# filters reason-frame
# ---------------------------------------------------------------------------


def test_filters_with_reasons_assigns_first_failing_filter():
    df = pl.DataFrame([
        _row("OK"),
        _row("HIST", data_quality_flag="INSUFFICIENT_HISTORY"),
        _row("POOR", data_quality_flag="POOR"),
        _row("CAT", cat="Liquid"),
        _row("CAPT", capture_efficiency=0.30),
        _row("IR", info_ratio_3y=-1.50),
        _row("R2", r_squared_3y=0.20),
        _row("BETA", beta_3y=2.10),
    ])
    survivors, excluded = apply_hard_filters(df, with_reasons=True)
    assert set(survivors["scheme_code"]) == {"OK"}
    reasons = dict(excluded.select("scheme_code", "exclusion_reason").rows())
    assert reasons == {
        "HIST": "INSUFFICIENT_HISTORY",
        "POOR": "FILTER:data_quality",
        "CAT": "FILTER:rankable_category",
        "CAPT": "FILTER:capture_efficiency",
        "IR": "FILTER:info_ratio",
        "R2": "FILTER:r_squared",
        "BETA": "FILTER:beta",
    }


def test_filters_with_reasons_priority_order():
    """A row failing several filters reports the FIRST one in application
    order (data quality before category before metric floors)."""
    df = pl.DataFrame([
        _row("OK"),
        _row("MULTI", data_quality_flag="INSUFFICIENT_HISTORY",
             cat="Liquid", capture_efficiency=0.30, beta_3y=2.10),
        _row("CAT_FIRST", cat="Liquid", r_squared_3y=0.20),
    ])
    _, excluded = apply_hard_filters(df, with_reasons=True)
    reasons = dict(excluded.select("scheme_code", "exclusion_reason").rows())
    assert reasons["MULTI"] == "INSUFFICIENT_HISTORY"
    assert reasons["CAT_FIRST"] == "FILTER:rankable_category"


def test_filters_with_reasons_null_category_excluded():
    df = pl.DataFrame([_row("OK"), _row("NOCAT", cat=None)])
    survivors, excluded = apply_hard_filters(df, with_reasons=True)
    assert set(survivors["scheme_code"]) == {"OK"}
    assert excluded["exclusion_reason"].to_list() == ["FILTER:rankable_category"]


def test_filters_with_reasons_survivors_match_default_return():
    df = pl.DataFrame([
        _row("A"),
        _row("B", capture_efficiency=None),  # null-tolerant: kept
        _row("C", r_squared_3y=0.20),
    ])
    default = apply_hard_filters(df)
    survivors, excluded = apply_hard_filters(df, with_reasons=True)
    assert survivors.select("scheme_code").rows() == default.select("scheme_code").rows()
    assert set(excluded["scheme_code"]) == {"C"}


def test_filters_with_reasons_empty_frame():
    survivors, excluded = apply_hard_filters(pl.DataFrame(), with_reasons=True)
    assert survivors.is_empty() and excluded.is_empty()


# ---------------------------------------------------------------------------
# build_rank_history — pure frame assembly
# ---------------------------------------------------------------------------


def _scored_stage1() -> pl.DataFrame:
    df = pl.DataFrame([
        _row("A", alpha_3y_annualized=0.06),
        _row("B", alpha_3y_annualized=0.02),
        _row("C", cat="Mid Cap"),
    ])
    from mfs.rank.score import composite_score_stage1
    from mfs.rank.zscore import zscore_within_category

    return composite_score_stage1(zscore_within_category(df))


def _excluded_stage1() -> pl.DataFrame:
    return pl.DataFrame([_row("X", r_squared_3y=0.20)]).with_columns(
        pl.lit("FILTER:r_squared").alias("exclusion_reason")
    )


def _stage2_frames() -> tuple[pl.DataFrame, pl.DataFrame]:
    survivors = pl.DataFrame([
        {**_row("A"), "stage2_rank": 1, "composite_score": 1.4,
         "composite_score_stage1": 1.1, "z_active_share_median_1y": 0.5},
        {**_row("C", cat="Mid Cap"), "stage2_rank": 1, "composite_score": 0.9,
         "composite_score_stage1": 0.7, "z_active_share_median_1y": -0.5},
    ])
    dropped = pl.DataFrame([
        {**_row("B"), "stage1_rank": 2,
         "missing_metrics": "ptr_latest;style_drift_3y"},
    ])
    return survivors, dropped


def test_build_rank_history_shape_and_reasons():
    s2_survivors, s2_dropped = _stage2_frames()
    s3_final = s2_survivors.filter(pl.col("scheme_code") == "A")
    s3_dropped = pl.DataFrame([{
        "scheme_code_dropped": "C", "scheme_name_dropped": None,
        "category_dropped": "Mid Cap", "stage2_rank_dropped": 1,
        "scheme_code_kept": "A", "overlap_pct": 45.0,
    }])
    out = shortlist.build_rank_history(
        AS_OF,
        stage1_scored=_scored_stage1(),
        stage1_excluded=_excluded_stage1(),
        stage2_survivors=s2_survivors,
        stage2_dropped=s2_dropped,
        stage3_final=s3_final,
    )
    assert out.columns == list(shortlist.RANK_HISTORY_SCHEMA)
    assert out["as_of_date"].unique().to_list() == [AS_OF]
    assert set(out["stage"].to_list()) == {1, 2, 3}
    # One row per (stage, scheme_code).
    assert out.height == out.select("stage", "scheme_code").unique().height

    s1 = out.filter(pl.col("stage") == 1)
    assert set(s1.filter(pl.col("included"))["scheme_code"]) == {"A", "B", "C"}
    x = s1.filter(pl.col("scheme_code") == "X").row(0, named=True)
    assert x["included"] is False
    assert x["exclusion_reason"] == "FILTER:r_squared"
    assert x["rank_in_category"] is None
    # Per-category rank ordered by composite_score within category.
    by_code = {r["scheme_code"]: r for r in s1.iter_rows(named=True)}
    assert by_code["A"]["rank_in_category"] == 1   # beats B in Large Cap
    assert by_code["B"]["rank_in_category"] == 2
    assert by_code["C"]["rank_in_category"] == 1   # alone in Mid Cap
    assert by_code["A"]["z_alpha_3y_annualized"] is not None
    assert by_code["A"]["composite_score"] is not None

    s2 = out.filter(pl.col("stage") == 2)
    a2 = s2.filter(pl.col("scheme_code") == "A").row(0, named=True)
    assert a2["included"] is True
    assert a2["rank_in_category"] == 1
    assert a2["composite_score"] == 1.4
    assert a2["composite_score_stage1"] == 1.1
    b2 = s2.filter(pl.col("scheme_code") == "B").row(0, named=True)
    assert b2["included"] is False
    assert b2["exclusion_reason"] == "MISSING_CORE_METRIC:ptr_latest"

    s3 = out.filter(pl.col("stage") == 3)
    # D4 keep-both: stage 3 never drops — history records are the final picks
    # only, all included; overlap breaches live in overlap_breaches.csv, not
    # as exclusion records.
    assert s3["scheme_code"].to_list() == ["A"]
    assert s3["included"].all()


def test_build_rank_history_all_empty_inputs():
    empty = pl.DataFrame()
    out = shortlist.build_rank_history(
        AS_OF,
        stage1_scored=empty, stage1_excluded=empty,
        stage2_survivors=empty, stage2_dropped=empty,
        stage3_final=empty,
    )
    assert out.is_empty()
    assert out.columns == list(shortlist.RANK_HISTORY_SCHEMA)


def test_build_rank_history_run_id_is_null_until_workstream_f():
    out = shortlist.build_rank_history(
        AS_OF,
        stage1_scored=_scored_stage1(),
        stage1_excluded=pl.DataFrame(),
        stage2_survivors=pl.DataFrame(),
        stage2_dropped=pl.DataFrame(),
        stage3_final=pl.DataFrame(),
    )
    assert out["run_id"].null_count() == out.height


# ---------------------------------------------------------------------------
# writers — fake-DB pattern from test_scheme_master
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self):
        self.statements: list[tuple[object, tuple | None]] = []
        self.upserts: list[tuple[str, list[str], list[tuple]]] = []
        self.connects = 0
        self.rowcount = 7


class _FakeCursor:
    def __init__(self, db: _FakeDB):
        self._db = db
        self.rowcount = db.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self._db.statements.append((query, params))


class _FakeConn:
    def __init__(self, db: _FakeDB):
        self._db = db

    def cursor(self):
        return _FakeCursor(self._db)


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDB()

    @contextlib.contextmanager
    def _fake_connect(autocommit=False):
        db.connects += 1
        yield _FakeConn(db)

    def _fake_copy_upsert(conn, table, columns, rows, pk):
        rows = list(rows)
        db.upserts.append((table, list(columns), rows))
        return len(rows)

    monkeypatch.setattr(writers, "connect", _fake_connect)
    monkeypatch.setattr(writers, "_copy_upsert", _fake_copy_upsert)
    return db


def _stmt_str(stmt) -> str:
    return stmt if isinstance(stmt, str) else repr(stmt)


def test_snapshot_scheme_master_delete_then_insert_select(fake_db):
    n = writers.snapshot_scheme_master(date(2026, 6, 12))
    assert n == fake_db.rowcount
    assert fake_db.connects == 1  # DELETE + INSERT in one transaction
    assert len(fake_db.statements) == 2
    (del_stmt, del_params), (ins_stmt, ins_params) = fake_db.statements
    assert "DELETE FROM scheme_master_history" in _stmt_str(del_stmt)
    assert del_params == (date(2026, 6, 12),)
    assert "INSERT INTO scheme_master_history" in _stmt_str(ins_stmt)
    assert "FROM scheme_master" in _stmt_str(ins_stmt)
    assert ins_params == (date(2026, 6, 12),)


def test_snapshot_scheme_master_rerun_replaces_partition(fake_db):
    writers.snapshot_scheme_master(date(2026, 6, 12))
    writers.snapshot_scheme_master(date(2026, 6, 12))
    deletes = [
        p for s, p in fake_db.statements
        if "DELETE FROM scheme_master_history" in _stmt_str(s)
    ]
    assert deletes == [(date(2026, 6, 12),), (date(2026, 6, 12),)]


def _history_frame() -> pl.DataFrame:
    return shortlist.build_rank_history(
        AS_OF,
        stage1_scored=_scored_stage1(),
        stage1_excluded=_excluded_stage1(),
        stage2_survivors=_stage2_frames()[0],
        stage2_dropped=_stage2_frames()[1],
        stage3_final=pl.DataFrame(),
    )


def test_persist_rank_history_delete_then_batch_insert(fake_db):
    df = _history_frame()
    n = writers.persist_rank_history(df, AS_OF)
    assert n == df.height
    assert fake_db.connects == 1  # DELETE + COPY-insert in one transaction
    (del_stmt, del_params) = fake_db.statements[0]
    assert "DELETE FROM rank_history" in _stmt_str(del_stmt)
    assert del_params == (AS_OF,)
    table, cols, rows = fake_db.upserts[0]
    assert table == "rank_history"
    assert cols == writers._RANK_HISTORY_COLS
    assert len(rows) == df.height
    i_inc = cols.index("included")
    i_reason = cols.index("exclusion_reason")
    assert all(
        (r[i_inc] is True and r[i_reason] is None)
        or (r[i_inc] is False and r[i_reason] is not None)
        for r in rows
    )


def test_persist_rank_history_rerun_replaces_partition(fake_db):
    df = _history_frame()
    writers.persist_rank_history(df, AS_OF)
    writers.persist_rank_history(df, AS_OF)
    deletes = [
        p for s, p in fake_db.statements
        if "DELETE FROM rank_history" in _stmt_str(s)
    ]
    assert deletes == [(AS_OF,), (AS_OF,)]
    assert len(fake_db.upserts) == 2


def test_persist_rank_history_empty_frame_writes_nothing(fake_db):
    assert writers.persist_rank_history(pl.DataFrame(), AS_OF) == 0
    assert fake_db.statements == []
    assert fake_db.upserts == []


def test_persist_rank_history_null_fills_missing_columns(fake_db):
    df = pl.DataFrame({
        "stage": [1], "scheme_code": ["A"], "included": [True],
    })
    writers.persist_rank_history(df, AS_OF)
    _, cols, rows = fake_db.upserts[0]
    row = dict(zip(cols, rows[0]))
    assert row["as_of_date"] == AS_OF
    assert row["composite_score"] is None
    assert row["run_id"] is None


# ---------------------------------------------------------------------------
# persist hooks — scheme_master.build() and shortlist.rank_deep
# ---------------------------------------------------------------------------


def test_build_snapshots_scheme_master_after_sync(monkeypatch):
    """build() must snapshot AFTER the diff-sync so the history records the
    post-sync state, stamped with today's date."""
    import mfs.db.connection as dbconn
    from mfs.ingest import amfi_nav
    from mfs.master import scheme_master

    snap = pl.DataFrame({
        "scheme_code": ["100"],
        "isin_growth": pl.Series([None], dtype=pl.Utf8),
        "isin_idcw": pl.Series([None], dtype=pl.Utf8),
        "scheme_name": ["Acme Large Cap Fund - Direct Plan - Growth"],
        "amc_name": ["Acme Mutual Fund"],
        "amfi_category": ["Open Ended Schemes(Large Cap Fund)"],
    })
    monkeypatch.setattr(amfi_nav, "fetch_today", lambda: b"raw")
    monkeypatch.setattr(amfi_nav, "parse_to_dataframe", lambda content: snap)

    class _Res:
        @staticmethod
        def fetchall():
            return []

    class _Conn:
        def execute(self, query, params=None):
            return _Res()

    @contextlib.contextmanager
    def _fake_connect(autocommit=False):
        yield _Conn()

    monkeypatch.setattr(dbconn, "connect", _fake_connect)

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        writers, "sync_scheme_master",
        lambda df: calls.append(("sync", df.height)) or df.height,
    )
    monkeypatch.setattr(
        writers, "snapshot_scheme_master",
        lambda d: calls.append(("snapshot", d)) or 0,
    )

    df = scheme_master.build()
    assert df.height == 1
    assert [name for name, _ in calls] == ["sync", "snapshot"]
    assert calls[1][1] == date.today()


def test_rank_deep_persists_history_at_tail(monkeypatch):
    """rank_deep must hand ONE batch to persist_rank_history covering all
    three stages, with included/exclusion_reason populated."""
    scored = _scored_stage1()
    excluded = _excluded_stage1()
    monkeypatch.setattr(
        shortlist, "build_scored_stage1",
        lambda as_of=None: (AS_OF, scored, excluded),
    )
    monkeypatch.setattr(shortlist, "_write_stage1_outputs", lambda s, e, a: {})
    monkeypatch.setattr(shortlist, "_build_aum_map", lambda s, as_of=None: {})
    monkeypatch.setattr(
        q, "computed_metrics_for_schemes",
        lambda as_of, codes: pl.DataFrame([_row(c) for c in sorted(codes)]),
    )
    monkeypatch.setattr(q, "scheme_master", lambda **kw: pl.DataFrame())

    s2_survivors, s2_dropped = _stage2_frames()
    stage2_result = {
        "stage2_dir": "stage2", "category_files": {}, "dropped_file": "d",
        "coverage_file": "c", "n_survivors": s2_survivors.height,
        "n_dropped": s2_dropped.height, "survivors": s2_survivors,
        "dropped": s2_dropped, "coverage": pl.DataFrame(),
    }
    s3_final = s2_survivors.filter(pl.col("scheme_code") == "A")
    s3_dropped = pl.DataFrame([{
        "scheme_code_dropped": "C", "category_dropped": "Mid Cap",
        "stage2_rank_dropped": 1, "scheme_code_kept": "A",
        "overlap_pct": 45.0,
    }])
    stage3_result = {
        "stage3_dir": "stage3", "category_files": {}, "breaches_file": "b",
        "overlap_pairs_file": "p", "overlap_matrix_file": "m",
        "n_initial": s2_survivors.height,
        "n_final": s3_final.height, "n_flagged": 1,
        "n_breach_pairs": s3_dropped.height,
        "final_picks": s3_final, "breaches": s3_dropped,
        "overlap_pairs": pl.DataFrame(), "overlap_matrix": pl.DataFrame(),
    }
    import mfs.rank.stage2 as stage2_mod
    import mfs.rank.stage3 as stage3_mod

    monkeypatch.setattr(stage2_mod, "run", lambda *a, **kw: stage2_result)
    monkeypatch.setattr(stage3_mod, "run", lambda *a, **kw: stage3_result)

    persisted: list[tuple[pl.DataFrame, date]] = []
    monkeypatch.setattr(
        writers, "persist_rank_history",
        lambda df, as_of: persisted.append((df, as_of)) or df.height,
    )

    result = shortlist.rank_deep(as_of=AS_OF, skip_phase2_compute=True)
    assert result["as_of"] == AS_OF.isoformat()
    assert len(persisted) == 1  # one batch per run
    df, as_of = persisted[0]
    assert as_of == AS_OF
    assert df.columns == list(shortlist.RANK_HISTORY_SCHEMA)
    assert set(df["stage"].to_list()) == {1, 2, 3}
    reasons = set(
        df.filter(~pl.col("included"))["exclusion_reason"].to_list()
    )
    assert reasons == {
        "FILTER:r_squared", "MISSING_CORE_METRIC:ptr_latest",
    }
    assert df.filter(pl.col("included"))["exclusion_reason"].null_count() == \
        df.filter(pl.col("included")).height


# ---------------------------------------------------------------------------
# queries readers — fake connection roundtrip
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_query_conn(monkeypatch):
    captured: dict = {"sql": None, "params": None, "rows": []}

    class _Res:
        @staticmethod
        def fetchall():
            return captured["rows"]

    class _Conn:
        def execute(self, query, params=None):
            captured["sql"] = query
            captured["params"] = params
            return _Res()

    @contextlib.contextmanager
    def _fake_connect(autocommit=False):
        yield _Conn()

    monkeypatch.setattr(q, "connect", _fake_connect)
    return captured


def test_rank_history_at_roundtrip(fake_query_conn):
    row = [None] * len(q._RANK_HISTORY_SCHEMA)
    cols = list(q._RANK_HISTORY_SCHEMA)
    row[cols.index("as_of_date")] = AS_OF
    row[cols.index("stage")] = 1
    row[cols.index("scheme_code")] = "A"
    row[cols.index("composite_score")] = 1.5
    row[cols.index("included")] = True
    fake_query_conn["rows"] = [tuple(row)]

    out = q.rank_history_at(AS_OF, stage=1)
    assert fake_query_conn["params"] == (AS_OF, 1)
    assert "AND stage = %s" in fake_query_conn["sql"]
    assert out.columns == cols
    assert out.row(0, named=True)["scheme_code"] == "A"
    assert out.row(0, named=True)["composite_score"] == 1.5


def test_rank_history_at_without_stage(fake_query_conn):
    fake_query_conn["rows"] = []
    out = q.rank_history_at(AS_OF)
    assert fake_query_conn["params"] == (AS_OF,)
    assert "AND stage" not in fake_query_conn["sql"]
    assert out.is_empty()


def test_rank_history_dates_roundtrip(fake_query_conn):
    fake_query_conn["rows"] = [(date(2026, 5, 8),), (date(2026, 6, 8),)]
    assert q.rank_history_dates() == [date(2026, 5, 8), date(2026, 6, 8)]


def test_scheme_master_history_at_roundtrip(fake_query_conn):
    cols = list(q._SCHEME_MASTER_HISTORY_SCHEMA)
    row = [None] * len(cols)
    row[cols.index("snapshot_date")] = AS_OF
    row[cols.index("scheme_code")] = "100"
    row[cols.index("scheme_name")] = "Acme Fund"
    row[cols.index("amc_name")] = "Acme"
    row[cols.index("amc_code")] = "acme"
    row[cols.index("plan_type")] = "DIRECT"
    row[cols.index("option_type")] = "GROWTH"
    row[cols.index("base_fund_id")] = "acme::fund"
    row[cols.index("is_active")] = True
    row[cols.index("last_seen_date")] = AS_OF
    fake_query_conn["rows"] = [tuple(row)]

    out = q.scheme_master_history_at(AS_OF)
    assert fake_query_conn["params"] == (AS_OF,)
    assert out.columns == cols
    assert out.row(0, named=True)["benchmark_ticker"] is None
    assert out.row(0, named=True)["is_active"] is True


def test_scheme_master_history_dates_roundtrip(fake_query_conn):
    fake_query_conn["rows"] = [(date(2026, 6, 11),)]
    assert q.scheme_master_history_dates() == [date(2026, 6, 11)]


# ---------------------------------------------------------------------------
# column-registry consistency (shortlist / writers / queries / schema.sql)
# ---------------------------------------------------------------------------


def _schema_table_cols(table: str) -> list[str]:
    sql = Path("src/mfs/db/schema.sql").read_text()
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.DOTALL
    )
    assert m, f"{table} not found in schema.sql"
    cols = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith(("PRIMARY KEY", "CONSTRAINT", "--")):
            continue
        cols.append(line.split()[0])
    return cols


def test_rank_history_registries_match_schema():
    ddl_cols = _schema_table_cols("rank_history")
    assert ddl_cols == writers._RANK_HISTORY_COLS
    assert ddl_cols == list(shortlist.RANK_HISTORY_SCHEMA)
    assert ddl_cols == list(q._RANK_HISTORY_SCHEMA)


def test_scheme_master_history_registries_match_schema():
    ddl_cols = _schema_table_cols("scheme_master_history")
    assert ddl_cols == ["snapshot_date", *writers._SCHEME_MASTER_HISTORY_COLS]
    assert ddl_cols == list(q._SCHEME_MASTER_HISTORY_SCHEMA)
