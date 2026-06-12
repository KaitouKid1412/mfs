"""C5: holdings parse-skip parity with the managers path.

When the fetched artifact set (every winner's printed name + Excel bytes) AND
the scheme-match universe are identical to the last successful ingest, and the
DB already holds the rows, ``run_for_amc`` must skip the parse loop AND the
per-month DELETE+reinsert (churn is the win). Any change — mutated Excel
bytes, a changed candidate universe, ``force=True``, or an empty DB — must
force a full re-parse. Modeled on tests/test_parse_cache.py +
tests/test_incremental_ingest.py (stubbed queries/writers/connect).
"""

from __future__ import annotations

from contextlib import contextmanager

import openpyxl
import polars as pl

import mfs.db.connection as dbconn
import mfs.io.http as mfs_http
import mfs.paths as mfs_paths
from mfs.ingest.holdings import _run as holdings_run
from mfs.ingest.holdings._generic import GenericHoldingsAdapter

_YM = "2026-05"
_AMC = "skipamc"


class _RecordingConn:
    """Scripts COUNT queries as 'nothing prior' and records every statement."""

    def __init__(self, sql_log: list[str]):
        self._sql_log = sql_log

    def execute(self, sql, params=None):
        self._sql_log.append(" ".join(sql.split()))

        class _Cur:
            def fetchone(self_inner):
                return (0,) if "count(distinct" in " ".join(sql.split()).lower() else None

        return _Cur()


def _write_portfolio_xlsx(path, prefix: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ISIN", "Name of the Instrument", "Industry", "% to NAV"])
    for i in range(6):
        ws.append([f"INE{i:03d}B0104{i % 10}", f"{prefix} {i}", "X", 16.5])
    wb.save(path)


class _SkipAdapter(GenericHoldingsAdapter):
    """Two fixture schemes; inherited fetch_excel (cached-by-existence)."""

    amc_slug = _AMC
    source_label = "Skip AMC"

    def __init__(self, parse_calls: list[str]):
        self._parse_calls = parse_calls

    def discover_scheme_urls(self, ym):
        return {
            "Skip Alpha Fund": "http://x.test/alpha.xlsx",
            "Skip Beta Fund": "http://x.test/beta.xlsx",
        }

    def parse_excel(self, excel_path, scheme_name_printed, ym):
        self._parse_calls.append(scheme_name_printed)
        return super().parse_excel(excel_path, scheme_name_printed, ym)


class _Env:
    """One fully-stubbed holdings environment around a tmp dir."""

    def __init__(self, monkeypatch, tmp_path):
        self.tmp_path = tmp_path
        self.parse_calls: list[str] = []
        self.sql_log: list[str] = []
        self.connect_calls = 0
        self.written: list[pl.DataFrame] = []
        self.db_has_rows = False
        self.sm = pl.DataFrame({
            "scheme_code": ["X01", "X02"],
            "scheme_name": ["Skip Alpha Fund", "Skip Beta Fund"],
            "amc_code": [_AMC, _AMC],
            "plan_type": ["DIRECT", "DIRECT"],
            "option_type": ["GROWTH", "GROWTH"],
            "is_active": [True, True],
            "canonical_category": ["Flexi Cap", "Flexi Cap"],
        })

        def fake_excel_raw(amc_slug, ym, scheme_filename):
            return tmp_path / "holdings" / amc_slug / ym / scheme_filename.replace("/", "_")

        monkeypatch.setattr(mfs_paths, "holdings_excel_raw", fake_excel_raw)
        self.excel_raw = fake_excel_raw

        def fake_download(url, out, expect=None):
            _write_portfolio_xlsx(out, "FreshCo")
            return out

        import mfs.ingest.holdings._generic as holdings_generic
        monkeypatch.setattr(holdings_generic, "download_to", fake_download)
        monkeypatch.setattr(mfs_http, "download_to", fake_download)

        adapter = _SkipAdapter(self.parse_calls)
        monkeypatch.setattr(holdings_run, "get_adapter", lambda slug: adapter)
        monkeypatch.setattr(holdings_run.q, "scheme_master", lambda **kw: self.sm)
        monkeypatch.setattr(
            holdings_run.q, "has_holdings_rows", lambda *a, **k: self.db_has_rows
        )
        monkeypatch.setattr(
            holdings_run.w, "upsert_holdings",
            lambda df, conn=None: self.written.append(df) or len(df),
        )

        env = self

        @contextmanager
        def fake_connect(autocommit=False):
            env.connect_calls += 1
            yield _RecordingConn(env.sql_log)

        monkeypatch.setattr(dbconn, "connect", fake_connect)

    def run(self, force=False):
        return holdings_run.run_for_amc(_AMC, ym=_YM, force=force)

    @property
    def marker(self):
        return self.excel_raw(_AMC, _YM, ".ingested.json")

    @property
    def n_deletes(self):
        return sum(1 for s in self.sql_log if s.upper().startswith("DELETE"))


def test_first_run_parses_writes_and_records_marker(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    res = env.run()
    assert sorted(env.parse_calls) == ["Skip Alpha Fund", "Skip Beta Fund"]
    assert res["rows_written"] == 12
    assert "skipped" not in res
    assert env.n_deletes == 1
    assert env.marker.exists()  # written only after the commit


def test_second_unchanged_run_skips_parse_and_delete(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    env.run()
    env.db_has_rows = True  # the first run's rows are now in the DB
    parse_before, deletes_before, connects_before = (
        len(env.parse_calls), env.n_deletes, env.connect_calls,
    )
    res = env.run()
    assert res["skipped"] is True
    assert res["rows_written"] == 0
    assert len(env.parse_calls) == parse_before     # parse_excel not invoked
    assert env.n_deletes == deletes_before          # no DELETE executed
    assert env.connect_calls == connects_before     # transaction never opened


def test_mutated_excel_bytes_trigger_full_reparse(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    env.run()
    env.db_has_rows = True
    _write_portfolio_xlsx(
        env.excel_raw(_AMC, _YM, "Skip Alpha Fund.xlsx"), "MutatedCo"
    )  # republished artifact, same path
    res = env.run()
    assert "skipped" not in res
    assert len(env.parse_calls) == 4  # both schemes re-parsed
    assert env.n_deletes == 2


def test_changed_candidate_universe_triggers_full_reparse(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    env.run()
    env.db_has_rows = True
    env.sm = env.sm.vstack(pl.DataFrame({
        "scheme_code": ["X03"],
        "scheme_name": ["Skip Gamma Fund"],
        "amc_code": [_AMC],
        "plan_type": ["DIRECT"],
        "option_type": ["GROWTH"],
        "is_active": [True],
        "canonical_category": ["Flexi Cap"],
    }))  # scheme_master changed → matches could re-route → must re-parse
    res = env.run()
    assert "skipped" not in res
    assert len(env.parse_calls) == 4


def test_force_bypasses_skip(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    env.run()
    env.db_has_rows = True
    res = env.run(force=True)
    assert "skipped" not in res
    assert len(env.parse_calls) == 4
    assert res["rows_written"] == 12


def test_never_skips_when_db_has_no_rows(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    env.run()
    env.db_has_rows = False  # marker present + bytes identical, but DB empty
    res = env.run()
    assert "skipped" not in res
    assert len(env.parse_calls) == 4
