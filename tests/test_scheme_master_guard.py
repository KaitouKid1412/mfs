"""F-12: category-relabel guard in scheme_master.build().

_canonical_category returns None silently for unmatched AMFI category
strings; combined with a rebuild, a SEBI/AMFI relabel would silently
evaporate whole categories. The guard must halt (PipelineError) when a
currently-populated category would drop to zero matches.
"""

from __future__ import annotations

import contextlib
from datetime import date

import polars as pl
import pytest

from mfs.db import writers
from mfs.errors import PipelineError
from mfs.master import scheme_master


# ---------------------------------------------------------------------------
# _canonical_category pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Open Ended Schemes(Large Cap Fund)", "Large Cap"),
        ("Equity Scheme - Flexi Cap Fund", "Flexi Cap"),
        ("Open Ended Schemes(Dynamic Asset Allocation or Balanced Advantage)",
         "Balanced Advantage"),
        ("Equity Scheme - ELSS", "ELSS"),
        # A plausible SEBI relabel — one space removed — must NOT match.
        ("Equity Scheme - Flexicap Fund", None),
        # Closed/interval types are deliberately excluded (not "unmatched").
        ("Close Ended Schemes(ELSS)", None),
        ("Interval Fund Schemes(Income)", None),
        (None, None),
        ("", None),
    ],
)
def test_canonical_category_pins(raw, expected):
    assert scheme_master._canonical_category(raw) == expected


# ---------------------------------------------------------------------------
# check_category_disappearance unit behavior
# ---------------------------------------------------------------------------

UNMATCHED = {"Equity Scheme - Flexicap Fund": 12}


def test_guard_raises_when_populated_category_vanishes():
    with pytest.raises(PipelineError) as exc:
        scheme_master.check_category_disappearance(
            new_counts={"Large Cap": 30},
            existing_counts={"Flexi Cap": 10, "Large Cap": 28},
            unmatched_raw=UNMATCHED,
        )
    msg = str(exc.value)
    assert "Flexi Cap" in msg
    assert "Equity Scheme - Flexicap Fund" in msg  # the relabeled raw string


def test_guard_threshold_is_module_constant():
    assert scheme_master.CATEGORY_GUARD_MIN_SCHEMES == 5
    # Below-threshold existing categories never trip the guard.
    scheme_master.check_category_disappearance(
        new_counts={},
        existing_counts={"Flexi Cap": 4},
        unmatched_raw=UNMATCHED,
    )
    # Exactly at threshold trips it.
    with pytest.raises(PipelineError):
        scheme_master.check_category_disappearance(
            new_counts={},
            existing_counts={"Flexi Cap": 5},
            unmatched_raw=UNMATCHED,
        )


def test_guard_passes_when_category_still_matched():
    scheme_master.check_category_disappearance(
        new_counts={"Flexi Cap": 1},
        existing_counts={"Flexi Cap": 10},
        unmatched_raw={},
    )


# ---------------------------------------------------------------------------
# build()-level guard with monkeypatched AMFI fetch + DB reads
# ---------------------------------------------------------------------------


def _snapshot(amfi_category: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "scheme_code": ["100"],
            "isin_growth": pl.Series([None], dtype=pl.Utf8),
            "isin_idcw": pl.Series([None], dtype=pl.Utf8),
            "scheme_name": ["Acme Flexi Cap Fund - Direct Plan - Growth"],
            "amc_name": ["Acme Mutual Fund"],
            "amfi_category": [amfi_category],
        }
    )


def _wire(monkeypatch, snap: pl.DataFrame, existing_rows: list[tuple]):
    """Monkeypatch AMFI fetch, the DB connection (nav bounds + category
    counts), and the writers; return the recorded writer calls."""
    import mfs.db.connection as dbconn
    from mfs.ingest import amfi_nav

    monkeypatch.setattr(amfi_nav, "fetch_today", lambda: b"raw")
    monkeypatch.setattr(amfi_nav, "parse_to_dataframe", lambda content: snap)

    class _Res:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Conn:
        def execute(self, query, params=None):
            if "GROUP BY canonical_category" in query:
                return _Res(existing_rows)
            return _Res([])  # nav_daily bounds query

    @contextlib.contextmanager
    def _fake_connect(autocommit=False):
        yield _Conn()

    monkeypatch.setattr(dbconn, "connect", _fake_connect)

    calls: list[str] = []
    monkeypatch.setattr(
        writers, "sync_scheme_master", lambda df: calls.append("sync") or df.height
    )
    monkeypatch.setattr(
        writers, "snapshot_scheme_master", lambda d: calls.append("snapshot") or 0
    )
    return calls


def test_build_halts_on_relabeled_category(monkeypatch):
    """DB has 10 active Flexi Cap schemes; the new snapshot's category string
    was relabeled so it matches nothing -> build must raise BEFORE syncing."""
    calls = _wire(
        monkeypatch,
        _snapshot("Equity Scheme - Flexicap Fund"),
        existing_rows=[("Flexi Cap", 10)],
    )
    with pytest.raises(PipelineError) as exc:
        scheme_master.build()
    assert "Flexi Cap" in str(exc.value)
    assert "Equity Scheme - Flexicap Fund" in str(exc.value)
    assert calls == []  # nothing was written


def test_build_passes_when_category_still_matches(monkeypatch):
    calls = _wire(
        monkeypatch,
        _snapshot("Equity Scheme - Flexi Cap Fund"),
        existing_rows=[("Flexi Cap", 10)],
    )
    df = scheme_master.build()
    assert df.height == 1
    assert df["canonical_category"].to_list() == ["Flexi Cap"]
    assert calls == ["sync", "snapshot"]
    assert df["last_seen_date"].to_list() == [date.today()]
