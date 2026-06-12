"""Per-portfolio weight-sum + min-holdings sanity gate at holdings ingest (B3).

Bounds are empirical (live-DB calibration 2026-06-12, 1,172 rankable
scheme-months: sums span 47.9-108.8, 0 outside [40, 115]): hard-skip outside
[40, 115], WARN-but-write in [40,60) / (105,115]. The audit's mis-scaled
months sat at 120-186%. The >= 5-rows floor for rankable non-FoF schemes is
the generic net for the Motilal-class contamination (4 poached ETF rows
written to an active equity fund).

No network, no live DB — gate matrix via check_portfolio_sanity directly,
plus end-to-end runner coverage (skip ⇒ no rows appended, summary counters)
and the managers/factsheet path (_resolve_holdings drops the whole printed
portfolio).
"""

from __future__ import annotations

from contextlib import contextmanager

import openpyxl
import polars as pl
from structlog.testing import capture_logs

from mfs.ingest.holdings import _run as holdings_run
from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.holdings._run import check_portfolio_sanity
from mfs.ingest.managers._run import _resolve_holdings
from mfs.schemas import ParsedHoldingRecord


def _verdict(total, n_rows=20, category="Flexi Cap", master_name="X Flexi Cap Fund"):
    return check_portfolio_sanity(
        scheme_code="S1",
        printed_name="X Fund",
        master_name=master_name,
        category=category,
        total_weight=total,
        n_rows=n_rows,
        amc_slug="testamc",
    )


# --- weight-sum matrix ------------------------------------------------------


def test_186_pct_skipped_and_logged():
    with capture_logs() as logs:
        assert _verdict(186.0) == "weight_sum_breach"
    ev = [e for e in logs if e["event"] == "holdings.weight_sum_breach"]
    assert len(ev) == 1 and ev[0]["log_level"] == "error"
    assert ev[0]["weight_sum"] == 186.0


def test_160_pct_skipped():
    assert _verdict(160.0) == "weight_sum_breach"


def test_99_pct_written():
    with capture_logs() as logs:
        assert _verdict(99.0) == "ok"
    assert logs == []


def test_48_pct_thematic_written_with_warn():
    # Live Thematic min is 47.9 — derivative/cash-heavy books lose their
    # ISIN-less rows at parse; must WARN, never skip.
    with capture_logs() as logs:
        assert _verdict(48.0, category="Thematic") == "ok"
    ev = [e for e in logs if e["event"] == "holdings.weight_sum_suspect"]
    assert len(ev) == 1 and ev[0]["log_level"] == "warning"


def test_108_pct_psu_written_with_warn():
    with capture_logs() as logs:
        assert _verdict(108.8, category="PSU") == "ok"
    assert any(e["event"] == "holdings.weight_sum_suspect" for e in logs)


def test_39_pct_skipped_115_boundary_written():
    assert _verdict(39.9) == "weight_sum_breach"
    assert _verdict(115.0) == "ok"  # boundary inclusive, warns
    assert _verdict(115.1) == "weight_sum_breach"


def test_non_rankable_exempt():
    # category None (ETF/debt/not in the ranked universe): anything goes.
    assert _verdict(186.0, category=None) == "ok"
    assert _verdict(5.0, n_rows=2, category=None) == "ok"


# --- min-holdings floor -----------------------------------------------------


def test_equity_scheme_with_4_rows_skipped():
    with capture_logs() as logs:
        assert _verdict(99.0, n_rows=4) == "too_few_holdings"
    ev = [e for e in logs if e["event"] == "holdings.too_few_holdings"]
    assert len(ev) == 1 and ev[0]["log_level"] == "error"


def test_fof_master_name_with_4_rows_written():
    assert _verdict(
        99.0, n_rows=4, master_name="X Multi Factor Passive Fund of Funds",
    ) == "ok"
    assert _verdict(99.0, n_rows=4, master_name="X Gold FoF") == "ok"


def test_fof_printed_name_poaching_equity_code_still_skipped():
    # Motilal-class contamination: printed FoF name matched to a NON-FoF
    # equity scheme_code. The MATCHED master name decides — 4 rows on an
    # active equity fund must be refused regardless of the printed name.
    assert check_portfolio_sanity(
        scheme_code="152651",
        printed_name="X Multi Factor Passive Fund of Funds",
        master_name="X Multi Cap Fund - Direct Plan - Growth",
        category="Multi Cap",
        total_weight=99.0,
        n_rows=4,
        amc_slug="testamc",
    ) == "too_few_holdings"


def test_5_rows_passes_floor():
    assert _verdict(99.0, n_rows=5) == "ok"


# --- end-to-end: holdings runner skips the scheme, counts it ----------------

_SLUG = "gateamc"
_OK_SCHEME = "Gate Flexi Cap Fund"
_BAD_SCHEME = "Gate Mid Cap Fund"


class _StubGateAdapter(GenericHoldingsAdapter):
    amc_slug = _SLUG
    source_label = "Gate AMC"

    def __init__(self, cache_dir, weights_by_scheme):
        self._cache_dir = cache_dir
        self._weights = weights_by_scheme

    def discover_scheme_urls(self, ym):
        return {name: f"http://x.test/{name}.xlsx" for name in self._weights}

    def fetch_excel(self, url, scheme_filename, ym):
        out = self._cache_dir / self.amc_slug / ym / scheme_filename
        out.parent.mkdir(parents=True, exist_ok=True)
        scheme = scheme_filename.rsplit(".", 1)[0]
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["MONTHLY PORTFOLIO STATEMENT AS ON 30 Apr 2026"])
        ws.append(["ISIN", "Name of the Instrument", "Industry", "% to NAV"])
        for i, wgt in enumerate(self._weights[scheme]):
            ws.append([f"INE{i:03d}A0103{i % 10}", f"Security {i}", "X", wgt])
        wb.save(out)
        return out


def _sm():
    return pl.DataFrame({
        "scheme_code": ["G1", "G2"],
        "scheme_name": [_OK_SCHEME, _BAD_SCHEME],
        "amc_code": [_SLUG, _SLUG],
        "plan_type": ["DIRECT", "DIRECT"],
        "option_type": ["GROWTH", "GROWTH"],
        "is_active": [True, True],
        "canonical_category": ["Flexi Cap", "Mid Cap"],
    })


@contextmanager
def _fake_connect(autocommit=False):
    class _Cur:
        def fetchone(self):
            return (0,)

    class _Conn:
        def execute(self, sql, params=None):
            return _Cur()

    yield _Conn()


def test_runner_skips_breaching_scheme_writes_the_rest(tmp_path, monkeypatch):
    # G1 sums ~99 (ok); G2 sums 186 (mis-scaled) → only G1's rows written,
    # G2 counted in the run summary.
    stub = _StubGateAdapter(tmp_path, {
        _OK_SCHEME: [50.0, 30.0, 10.0, 5.0, 4.0],
        _BAD_SCHEME: [80.0, 60.0, 30.0, 10.0, 6.0],
    })
    written = []
    monkeypatch.setattr(holdings_run, "get_adapter", lambda slug: stub)
    monkeypatch.setattr(holdings_run.q, "scheme_master", lambda **kw: _sm())
    monkeypatch.setattr(
        holdings_run.w, "upsert_holdings",
        lambda df, conn=None: written.append(df) or len(df),
    )
    import mfs.db.connection as dbconn
    monkeypatch.setattr(dbconn, "connect", _fake_connect)

    res = holdings_run.run_for_amc(_SLUG, ym="2026-04")
    assert res["weight_sum_skipped_schemes"] == [_BAD_SCHEME]
    assert res["too_few_holdings_skipped_schemes"] == []
    codes = set(written[0]["scheme_code"].to_list())
    assert codes == {"G1"}
    assert res["rows_written"] == 5


def test_runner_skips_too_few_holdings_scheme(tmp_path, monkeypatch):
    # G2 is an active equity fund carrying exactly 4 rows (the contaminated
    # Motilal shape) → skipped + counted.
    stub = _StubGateAdapter(tmp_path, {
        _OK_SCHEME: [50.0, 30.0, 10.0, 5.0, 4.0],
        _BAD_SCHEME: [40.0, 30.0, 20.0, 9.0],
    })
    written = []
    monkeypatch.setattr(holdings_run, "get_adapter", lambda slug: stub)
    monkeypatch.setattr(holdings_run.q, "scheme_master", lambda **kw: _sm())
    monkeypatch.setattr(
        holdings_run.w, "upsert_holdings",
        lambda df, conn=None: written.append(df) or len(df),
    )
    import mfs.db.connection as dbconn
    monkeypatch.setattr(dbconn, "connect", _fake_connect)

    res = holdings_run.run_for_amc(_SLUG, ym="2026-04")
    assert res["too_few_holdings_skipped_schemes"] == [_BAD_SCHEME]
    assert set(written[0]["scheme_code"].to_list()) == {"G1"}


# --- managers/factsheet path: _resolve_holdings drops the printed portfolio --

_CANDIDATES = {
    "ABC FLEXI CAP FUND": {
        "scheme_code": "F1",
        "scheme_name": "ABC Flexi Cap Fund - Direct Plan - Growth",
    },
    "ABC MID CAP FUND": {
        "scheme_code": "F2",
        "scheme_name": "ABC Mid Cap Fund - Direct Plan - Growth",
    },
}
_CATS = {"F1": "Flexi Cap", "F2": "Mid Cap"}


def _rec(name, isin, weight):
    return ParsedHoldingRecord(
        scheme_name_printed=name,
        security_name=f"Sec {isin}",
        weight_pct=weight,
        isin=isin,
        instrument_type="Equity",
        source_amc="abc",
    )


def test_factsheet_path_drops_breaching_scheme_keeps_other():
    records = (
        [_rec("ABC Flexi Cap Fund", f"INE00{i}A0100{i}", 19.8) for i in range(5)]
        + [_rec("ABC Mid Cap Fund", f"INE10{i}A0101{i}", 37.2) for i in range(5)]
    )  # Flexi sums 99 (ok); Mid sums 186 (breach)
    gate_skipped: dict[str, list[str]] = {}
    out = _resolve_holdings(
        records, _CANDIDATES, "abc", "2026-04",
        cat_by_code=_CATS, gate_skipped=gate_skipped,
    )
    assert {r["scheme_code"] for r in out} == {"F1"}
    assert len(out) == 5
    assert gate_skipped["weight_sum_breach"] == ["ABC Mid Cap Fund"]


def test_factsheet_path_without_categories_is_ungated():
    # cat_by_code omitted (legacy/unit callers): no gate, behavior unchanged.
    records = [_rec("ABC Mid Cap Fund", f"INE10{i}A0101{i}", 37.2) for i in range(5)]
    out = _resolve_holdings(records, _CANDIDATES, "abc", "2026-04")
    assert len(out) == 5
