"""Collision-resolution tests for the factsheet orchestrator's resolvers (B5).

Two printed names fuzzy-matching the SAME scheme_code must be resolved by
match score (highest wins, loser dropped + logged) — never by parse order —
mirroring the holdings orchestrator's best-score-per-code rule. Also pins
the previously-dead ``match_threshold`` parameter: the resolvers must pass
it through to ``match_one``.

No network, no DB: the resolvers are pure given a candidate index.
"""

from __future__ import annotations

from datetime import date

from mfs.ingest.managers._run import _resolve_holdings, _resolve_ptr
from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord

_AMC = "abc"
_YM = "2026-04"

# One DIRECT+GROWTH candidate. "ABC Flexi Cap Fund" canonicalizes to an exact
# key match (score 100); "ABC Flexi Cap Fundd" (typo) fuzzy-matches the same
# code at a lower score (~97).
_CANDIDATES = {
    "ABC FLEXI CAP FUND": {
        "scheme_code": "F1",
        "scheme_name": "ABC Flexi Cap Fund - Direct Plan - Growth",
    },
}


def _ptr(name: str, ptr: float) -> ParsedPtrRecord:
    return ParsedPtrRecord(scheme_name_printed=name, ptr=ptr, source_amc=_AMC)


def _holding(name: str, security: str, weight: float) -> ParsedHoldingRecord:
    return ParsedHoldingRecord(
        scheme_name_printed=name,
        security_name=security,
        weight_pct=weight,
        isin="INE000A01001",
        instrument_type="EQUITY",
        source_amc=_AMC,
    )


def test_ptr_collision_keeps_higher_score_not_parse_order():
    # Lower-scoring (typo) name parses FIRST; the exact name must still win.
    records = [
        _ptr("ABC Flexi Cap Fundd", 99.0),
        _ptr("ABC Flexi Cap Fund", 42.0),
    ]
    out = _resolve_ptr(records, _CANDIDATES, _AMC, _YM)
    assert len(out) == 1
    assert out[0]["scheme_code"] == "F1"
    assert out[0]["ptr"] == 42.0
    assert out[0]["as_of_month"] == date(2026, 4, 1)


def test_ptr_collision_loser_is_logged_loudly():
    from structlog.testing import capture_logs

    records = [
        _ptr("ABC Flexi Cap Fundd", 99.0),
        _ptr("ABC Flexi Cap Fund", 42.0),
    ]
    with capture_logs() as logs:
        _resolve_ptr(records, _CANDIDATES, _AMC, _YM)
    events = [e for e in logs if e["event"] == "managers.match_collision_dropped"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert events[0]["names"] == ["ABC Flexi Cap Fundd"]


def test_holdings_collision_drops_all_loser_rows():
    # The losing printed name's ENTIRE portfolio is dropped — otherwise two
    # funds' holdings merge onto one scheme_code and weights sum to ~200%.
    records = [
        _holding("ABC Flexi Cap Fundd", "Poacher Security A", 50.0),
        _holding("ABC Flexi Cap Fundd", "Poacher Security B", 50.0),
        _holding("ABC Flexi Cap Fund", "True Security", 10.0),
    ]
    out = _resolve_holdings(records, _CANDIDATES, _AMC, _YM)
    assert [r["security_name"] for r in out] == ["True Security"]
    assert out[0]["scheme_code"] == "F1"


def test_match_threshold_is_passed_through():
    # threshold=99 rejects the ~97-scoring typo name (previously the
    # parameter was dead and DEFAULT_THRESHOLD always applied); the exact
    # name still matches via canonical equality.
    records = [_ptr("ABC Flexi Cap Fundd", 99.0), _ptr("ABC Flexi Cap Fund", 42.0)]
    out = _resolve_ptr(records, _CANDIDATES, _AMC, _YM, match_threshold=99)
    assert [r["ptr"] for r in out] == [42.0]

    holdings = [_holding("ABC Flexi Cap Fundd", "Poacher Security A", 50.0)]
    assert _resolve_holdings(holdings, _CANDIDATES, _AMC, _YM, match_threshold=99) == []
