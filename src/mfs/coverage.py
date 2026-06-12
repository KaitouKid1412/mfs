"""Per-source data-coverage contracts and gates.

This is the generalization of the RBI stale-feed incident: for every ingested
table we declare a *contract* and, before compute, answer six questions:

  1. need-from    — earliest date a downstream metric needs this source for
  2. have-from    — earliest date actually in the table
  3. have-till    — latest date actually in the table
  4. fresh-by     — oldest acceptable `have-till` (staleness cutoff)
  5. expected     — how many datapoints we expect (entities, or a calendar span)
  6. gap          — expected vs actual, plus how to fix it

Two gates run the contracts:

  * Gate A (BLOCKING) — NAV, benchmarks, risk-free, scheme_master. A breach
    (empty / stale / too-short-history / catastrophically low entity coverage /
    interior gap in the NAV calendar) raises ``CoverageError`` and halts the
    pipeline before compute. Runs right after scheme_master is built, before
    the expensive Phase-2 ingest.
  * Gate B (ADVISORY) — holdings, PTR, AAUM, constituents, stock-ADV. A breach
    is reported loudly and the affected funds are excluded downstream, but the
    run continues. Runs after Phase-2 ingest, before compute. This is the ONLY
    coverage net for these sources, since their freshness gates are disabled
    (null) in pipeline.yaml.

The engine answers the six questions; ``render`` turns a report into a bordered
text block printed to stdout (NOT through structlog, which would flatten a table
into one line).

``computed_metrics`` and ``fund_log_returns`` are derived artifacts, not ingested
sources, so they carry no contract (see ``OUT_OF_CONTRACT``); a registry-
completeness test asserts every schema table is either contracted or explicitly
out-of-contract, so a new ingest source can't ship without coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from mfs.config import get_pipeline_config
from mfs.db import queries as q
from mfs.db.connection import connect
from mfs.errors import CoverageError
from mfs.freshness import last_business_day
from mfs.utils.logging import get_logger

log = get_logger(__name__)

# --- Cadence / severity / status enums (plain strings) ---------------------
TRADING_DAY = "trading_day"
WEEKLY = "weekly"
MONTHLY = "monthly"
QUARTERLY = "quarterly"
SNAPSHOT = "snapshot"

BLOCKING = "BLOCKING"
ADVISORY = "ADVISORY"

# Status values
OK = "OK"
EMPTY = "EMPTY"
STALE = "STALE"
SHORT_HISTORY = "SHORT_HISTORY"
GAP = "GAP"
INTERIOR_GAP = "INTERIOR_GAP"

# Catastrophic entity-coverage floor for BLOCKING sources: below this fraction
# the ingest is considered broken (wrote almost nothing) rather than merely
# having a few stale funds, so we halt. Above it, low coverage is reported but
# does not block (a handful of stale funds shouldn't abort the run).
_BLOCKING_ENTITY_FLOOR = 0.5
# Slack for the short-history check (data may start a few days after need-from).
_HISTORY_TOLERANCE_DAYS = 7
# Default staleness lags (days) when a source's pipeline.yaml lag is null.
_DEFAULT_LAG_DAYS = {
    TRADING_DAY: 7, WEEKLY: 14, MONTHLY: 45, QUARTERLY: 120, SNAPSHOT: 2,
}

# Tables that are derived artifacts, not ingested sources — no coverage contract.
# scheme_master_history / rank_history are D2's per-run point-in-time snapshots.
OUT_OF_CONTRACT = frozenset({
    "computed_metrics", "fund_log_returns",
    "scheme_master_history", "rank_history",
})


@dataclass(frozen=True)
class Contract:
    table: str
    date_col: str
    cadence: str
    severity: str
    gate: str                       # 'A' or 'B'
    label: str
    need_back: tuple[str, int]      # ('years'|'months'|'days', n)
    lag_attr: str | None            # FreshnessConfig attr giving the staleness lag
    lag_unit: str                   # 'bdays' | 'days'
    entity_col: str | None = None   # None = singleton series
    entity_set: str | None = None   # 'rankable' | 'benchmark_tickers' | None
    accepted_missing: frozenset[str] = field(default_factory=frozenset)
    remediation: str = ""
    # Daily series with a known-density calendar can also be checked for holes
    # BETWEEN have-from and have-till (the STALE check only sees end-recency).
    # Currently nav_daily only; gated by freshness.max_nav_interior_gap_days.
    interior_gap: bool = False


CONTRACTS: list[Contract] = [
    # ---- Gate A: BLOCKING (daily / master) ----
    Contract(
        table="nav_daily", date_col="nav_date", cadence=TRADING_DAY,
        severity=BLOCKING, gate="A", label="NAV (nav_daily)",
        need_back=("years", 5), lag_attr="max_nav_lag_bdays", lag_unit="bdays",
        entity_col="scheme_code", entity_set="rankable",
        remediation="mfs ingest navs --backfill  (or --since YYYY-MM-DD)",
        interior_gap=True,
    ),
    Contract(
        table="benchmark_daily", date_col="date", cadence=TRADING_DAY,
        severity=BLOCKING, gate="A", label="Benchmarks (benchmark_daily)",
        need_back=("years", 5), lag_attr="max_bench_lag_bdays", lag_unit="bdays",
        entity_col="ticker", entity_set="benchmark_tickers",
        remediation="mfs ingest benchmarks",
    ),
    Contract(
        table="risk_free_daily", date_col="date", cadence=WEEKLY,
        severity=BLOCKING, gate="A", label="Risk-free (risk_free_daily)",
        need_back=("years", 5), lag_attr="max_rf_lag_days", lag_unit="days",
        remediation="mfs ingest tbill  (probe-forward discovery is automatic)",
    ),
    Contract(
        table="scheme_master", date_col="last_seen_date", cadence=SNAPSHOT,
        severity=BLOCKING, gate="A", label="Scheme master (scheme_master)",
        need_back=("days", 1), lag_attr="max_scheme_master_lag_days", lag_unit="days",
        remediation="mfs build scheme-master",
    ),
    # ---- Gate B: ADVISORY (monthly / quarterly signals) ----
    Contract(
        table="holdings_monthly", date_col="as_of_month", cadence=MONTHLY,
        severity=ADVISORY, gate="B", label="Holdings (holdings_monthly)",
        need_back=("months", 12), lag_attr="max_holdings_lag_days", lag_unit="days",
        entity_col="scheme_code", entity_set="rankable",
        remediation="mfs ingest managers  /  mfs ingest holdings",
    ),
    Contract(
        table="portfolio_turnover_monthly", date_col="as_of_month", cadence=MONTHLY,
        severity=ADVISORY, gate="B", label="PTR (portfolio_turnover_monthly)",
        need_back=("months", 12), lag_attr="max_ptr_lag_days", lag_unit="days",
        entity_col="scheme_code", entity_set="rankable",
        remediation="mfs ingest managers  (note: quant reports PTR annually)",
    ),
    Contract(
        table="scheme_aum_monthly", date_col="as_of_month", cadence=QUARTERLY,
        severity=ADVISORY, gate="B", label="AAUM (scheme_aum_monthly)",
        need_back=("months", 6), lag_attr="max_aum_lag_days", lag_unit="days",
        entity_col="scheme_code", entity_set="rankable",
        remediation="mfs ingest amfi-aum",
    ),
    Contract(
        table="index_constituents_monthly", date_col="as_of_month", cadence=MONTHLY,
        severity=ADVISORY, gate="B", label="Constituents (index_constituents_monthly)",
        need_back=("months", 12), lag_attr="max_constituents_lag_days", lag_unit="days",
        entity_col="ticker", entity_set="benchmark_tickers",
        # NIFTY100 ESG TRI has no exact passive tracker (coverage_to_95.md) — it
        # can never reach 100%, so it's an accepted exception, not a gap.
        accepted_missing=frozenset({"NIFTY100 ESG TRI"}),
        remediation="tools/derive_constituents.py  then  mfs ingest constituents",
    ),
    Contract(
        table="stock_adv_daily", date_col="date", cadence=TRADING_DAY,
        severity=ADVISORY, gate="B", label="Stock ADV (stock_adv_daily)",
        need_back=("days", 90), lag_attr="max_stock_adv_lag_days", lag_unit="days",
        remediation="mfs ingest bhavcopy",
    ),
]


@dataclass
class CoverageResult:
    source: str
    label: str
    cadence: str
    severity: str
    need_from: date
    have_from: date | None
    have_till: date | None
    fresh_by: date              # staleness cutoff (oldest acceptable have_till)
    n_rows: int
    n_expected: int | None      # expected entities (None for singleton series)
    n_present: int | None       # entities with fresh data
    status: str
    ok: bool                    # contract passes (BLOCKING uses this to halt)
    remediation: str
    detail: str = ""


@dataclass
class CoverageReport:
    as_of: date
    gate: str
    results: list[CoverageResult] = field(default_factory=list)

    @property
    def blocking_failures(self) -> list[CoverageResult]:
        return [r for r in self.results if r.severity == BLOCKING and not r.ok]

    @property
    def advisory_gaps(self) -> list[CoverageResult]:
        return [r for r in self.results if r.severity == ADVISORY and r.status != OK]

    def raise_if_blocking(self) -> None:
        fails = self.blocking_failures
        if not fails:
            return
        lines = ["BLOCKING coverage failure — refusing to compute on incomplete inputs:"]
        for r in fails:
            lines.append(f"  - {r.label}: {r.status} — {r.detail}")
            lines.append(f"    fix: {r.remediation}")
        raise CoverageError("\n".join(lines))


# --- pure helpers (no DB; unit-tested) -------------------------------------

def _need_from(need_back: tuple[str, int], as_of: date) -> date:
    unit, n = need_back
    if unit == "years":
        return as_of - timedelta(days=int(365.25 * n))
    if unit == "months":
        return as_of - timedelta(days=31 * n)
    return as_of - timedelta(days=n)


def _bday_shift_back(d: date, n: int) -> date:
    """Return the date `n` business days before `d` (weekday-only, no holidays)."""
    cur = d
    cnt = 0
    while cnt < n:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            cnt += 1
    return cur


def _stale_cutoff(contract: Contract, as_of: date, cfg) -> date:
    """Oldest acceptable `have_till`. Data older than this is STALE."""
    lag = getattr(cfg, contract.lag_attr, None) if contract.lag_attr else None
    if lag is None:
        lag = _DEFAULT_LAG_DAYS[contract.cadence]
    if contract.lag_unit == "bdays":
        return _bday_shift_back(last_business_day(as_of), lag)
    return as_of - timedelta(days=lag)


def _classify(
    *, severity: str, need_back_unit: str, need_from: date,
    have_from: date | None, have_till: date | None, stale_cutoff: date,
    n_expected: int | None, n_present: int | None,
) -> tuple[str, bool]:
    """Pure status + pass/fail decision. Returns (status, ok)."""
    if have_till is None:
        status = EMPTY
    elif have_till < stale_cutoff:
        status = STALE
    elif (
        need_back_unit == "years"  # only long-history series need the span check
        and have_from is not None
        and have_from > need_from + timedelta(days=_HISTORY_TOLERANCE_DAYS)
    ):
        status = SHORT_HISTORY
    elif n_expected and n_present is not None and n_present < n_expected:
        status = GAP
    else:
        status = OK

    if severity == BLOCKING:
        ok = status not in (EMPTY, STALE, SHORT_HISTORY)
        # Catastrophically low entity coverage ⇒ ingest is broken ⇒ halt.
        if (
            ok and n_expected and n_present is not None
            and (n_present / n_expected) < _BLOCKING_ENTITY_FLOOR
        ):
            return GAP, False
        return status, ok
    # Advisory never halts the run; status still reflects the gap.
    return status, True


# --- DB helpers (trusted table/col constants from CONTRACTS) ---------------

def _coerce_date(v):
    return v.date() if v is not None and hasattr(v, "date") else v


def _bounds(table: str, date_col: str) -> tuple[date | None, date | None, int]:
    with connect() as c:
        row = c.execute(
            f"SELECT MIN({date_col}), MAX({date_col}), COUNT(*) FROM {table}"
        ).fetchone()
    mn, mx, n = row
    return _coerce_date(mn), _coerce_date(mx), int(n or 0)


def _nav_interior_gap_days(
    end: date, n_days: int = 30, floor_frac: float = 0.5
) -> list[date]:
    """Trading days (NIFTY 50 TRI calendar) among the last `n_days` ending at
    `end` whose nav_daily row count falls below ``floor_frac`` × the median
    daily count over the trailing 3 years (median ~8,600 → floor ~4,300).

    This is the interior-hole detector the end-recency STALE check is blind to;
    pass ``end = have_till`` so end-staleness stays the STALE check's job.
    Legitimate special sessions (Muhurat / budget Saturdays, ~600–1,100 rows)
    also trip the floor, at a base rate of ≤1 per any 30-trading-day span —
    the configured ``max_nav_interior_gap_days`` threshold allows for that.
    """
    sql = """
        WITH cal AS (
            SELECT DISTINCT date AS d
            FROM benchmark_daily
            WHERE ticker = 'NIFTY 50 TRI' AND date <= %s
            ORDER BY d DESC
            LIMIT %s
        ),
        daily AS (
            SELECT nav_date, COUNT(*) AS cnt
            FROM nav_daily
            WHERE nav_date >= (SELECT MIN(d) FROM cal)
            GROUP BY nav_date
        ),
        floor_calc AS (
            SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY cnt) AS median_cnt
            FROM (
                SELECT COUNT(*) AS cnt
                FROM nav_daily
                WHERE nav_date > %s - INTERVAL '3 years'
                  AND nav_date <= %s
                  AND nav_date IN (
                      SELECT date FROM benchmark_daily WHERE ticker = 'NIFTY 50 TRI'
                  )
                GROUP BY nav_date
            ) t
        )
        SELECT cal.d
        FROM cal
        LEFT JOIN daily ON daily.nav_date = cal.d
        WHERE COALESCE(daily.cnt, 0) < %s * (SELECT median_cnt FROM floor_calc)
        ORDER BY cal.d
    """
    with connect() as c:
        rows = c.execute(sql, (end, n_days, end, end, floor_frac)).fetchall()
    return [_coerce_date(r[0]) for r in rows]


def _entity_latest(table: str, entity_col: str, date_col: str) -> dict[str, date]:
    with connect() as c:
        rows = c.execute(
            f"SELECT {entity_col}, MAX({date_col}) FROM {table} GROUP BY {entity_col}"
        ).fetchall()
    return {e: _coerce_date(m) for e, m in rows}


def _rankable_universe_df():
    """The funds the pipeline actually ranks: DIRECT + GROWTH + active, AND in a
    rankable (equity) category. The category filter is essential — without it the
    denominator balloons to every DIRECT+GROWTH fund (debt/liquid/index, ~2500)
    rather than the ~665 equity funds Stage 1 ranks, turning Gate A into a
    permanent false alarm."""
    from mfs.config import get_thresholds

    df = q.scheme_master(plan_type="DIRECT", option_type="GROWTH", is_active=True)
    if df.is_empty():
        return df
    cats = set(get_thresholds().get("rankable_categories", []))
    if cats:
        df = df.filter(pl.col("canonical_category").is_in(list(cats)))
    return df


def _entity_set(name: str) -> set[str]:
    df = _rankable_universe_df()
    if df.is_empty():
        return set()
    if name == "rankable":
        return set(df["scheme_code"].to_list())
    if name == "benchmark_tickers":
        # Only the benchmarks the rankable (equity) universe actually references —
        # not every active fund's benchmark (which would include debt-index TRIs
        # the pipeline never ingests, producing phantom gaps).
        return {t for t in df["benchmark_ticker"].to_list() if t}
    return set()


def evaluate(contract: Contract, as_of: date, cfg) -> CoverageResult:
    """Compute the six-question coverage result for one contract."""
    need_from = _need_from(contract.need_back, as_of)
    stale_cutoff = _stale_cutoff(contract, as_of, cfg)
    have_from, have_till, n_rows = _bounds(contract.table, contract.date_col)

    n_expected = n_present = None
    if contract.entity_col and contract.entity_set:
        expected = _entity_set(contract.entity_set) - set(contract.accepted_missing)
        n_expected = len(expected)
        latest = _entity_latest(contract.table, contract.entity_col, contract.date_col)
        n_present = sum(
            1 for e in expected
            if latest.get(e) is not None and latest[e] >= stale_cutoff
        )

    status, ok = _classify(
        severity=contract.severity, need_back_unit=contract.need_back[0],
        need_from=need_from, have_from=have_from, have_till=have_till,
        stale_cutoff=stale_cutoff, n_expected=n_expected, n_present=n_present,
    )
    detail = _detail(status, have_from, have_till, stale_cutoff, need_from,
                     n_present, n_expected, n_rows)
    remediation = contract.remediation

    # Interior-gap check: holes BETWEEN have-from and have-till that every
    # end-recency check (STALE) and per-entity-latest check (GAP counts) is
    # blind to. Only runs when the contract opts in (nav_daily), the series
    # already passed the harder checks (OK/GAP), and the threshold is set
    # (None disables — the rollback switch).
    max_gap_days = getattr(cfg, "max_nav_interior_gap_days", None)
    if contract.interior_gap and max_gap_days is not None and status in (OK, GAP):
        gap_days = _nav_interior_gap_days(have_till)
        if len(gap_days) > max_gap_days:
            status = INTERIOR_GAP
            ok = contract.severity != BLOCKING
            detail = (
                f"{len(gap_days)} sub-floor trading day(s) in the last 30 "
                f"(max allowed {max_gap_days}): "
                + ", ".join(d.isoformat() for d in gap_days)
            )
            remediation = f"mfs ingest navs --since {gap_days[0].isoformat()}"

    return CoverageResult(
        source=contract.table, label=contract.label, cadence=contract.cadence,
        severity=contract.severity, need_from=need_from, have_from=have_from,
        have_till=have_till, fresh_by=stale_cutoff, n_rows=n_rows,
        n_expected=n_expected, n_present=n_present, status=status, ok=ok,
        remediation=remediation, detail=detail,
    )


def _detail(status, have_from, have_till, stale_cutoff, need_from,
            n_present, n_expected, n_rows) -> str:
    if status == EMPTY:
        return "table is empty"
    if status == STALE:
        return f"latest={have_till} is older than required {stale_cutoff}"
    if status == SHORT_HISTORY:
        return f"history starts {have_from} but metrics need data from {need_from}"
    if status == GAP:
        if n_expected:
            miss = n_expected - (n_present or 0)
            return f"{n_present}/{n_expected} entities present ({miss} missing/stale)"
        return "coverage gap"
    if n_expected:
        return f"{n_present}/{n_expected} entities present"
    return f"{n_rows} rows, latest {have_till}"


# --- gate + rendering ------------------------------------------------------

def run_gate(gate: str, as_of: date | None = None, *, raise_on_block: bool = True) -> CoverageReport:
    """Evaluate every contract for `gate`. Raises CoverageError on a BLOCKING
    breach when raise_on_block=True (the caller prints the report first)."""
    as_of = as_of or date.today()
    cfg = get_pipeline_config().freshness
    report = CoverageReport(as_of=as_of, gate=gate)
    for c in CONTRACTS:
        if c.gate != gate:
            continue
        try:
            report.results.append(evaluate(c, as_of, cfg))
        except Exception as e:  # noqa: BLE001 — a query failure is itself a coverage failure
            log.error("coverage.evaluate_failed", source=c.table, err=str(e))
            report.results.append(CoverageResult(
                source=c.table, label=c.label, cadence=c.cadence,
                severity=c.severity, need_from=_need_from(c.need_back, as_of),
                have_from=None, have_till=None, fresh_by=as_of, n_rows=0,
                n_expected=None, n_present=None, status=EMPTY,
                ok=(c.severity != BLOCKING), remediation=c.remediation,
                detail=f"coverage query failed: {e}",
            ))
    log.info(
        "coverage.gate", gate=gate,
        blocking_failures=[r.source for r in report.blocking_failures],
        advisory_gaps=[r.source for r in report.advisory_gaps],
    )
    if raise_on_block:
        report.raise_if_blocking()
    return report


def _cover_cell(r: CoverageResult) -> str:
    if r.n_expected is not None:
        return f"{r.n_present}/{r.n_expected} funds"
    return f"{r.n_rows} rows"


def render(report: CoverageReport) -> str:
    """Render a report as a bordered, scannable text block for stdout."""
    title = "BLOCKING" if report.gate == "A" else "ADVISORY"
    bar = "=" * 78
    lines = [
        bar,
        f"DATA COVERAGE — GATE {report.gate} ({title})   as_of={report.as_of}",
        bar,
        f"{'source':<34}{'have-from':<12}{'have-till':<12}{'fresh-by':<12}"
        f"{'coverage':<16}status",
    ]
    for r in report.results:
        flag = f"[{r.status}]"
        lines.append(
            f"{r.label:<34}{str(r.have_from):<12}{str(r.have_till):<12}"
            f"{str(r.fresh_by):<12}{_cover_cell(r):<16}{flag}"
        )
        if r.status != OK:
            lines.append(f"    └ {r.detail}")
            if r.severity == ADVISORY:
                lines.append(f"      fix: {r.remediation}")
    bf = report.blocking_failures
    ag = report.advisory_gaps
    lines.append("-" * 78)
    if report.gate == "A":
        lines.append(
            f"GATE A {'FAILED' if bf else 'PASSED'} — "
            f"{len(report.results) - len(bf)}/{len(report.results)} blocking sources OK"
        )
    else:
        ok_n = len(report.results) - len(ag)
        lines.append(
            f"GATE B — {ok_n}/{len(report.results)} sources fully covered; "
            f"{len(ag)} with gaps (affected funds excluded from those metrics)"
        )
    return "\n".join(lines)


def render_summary(reports: list[CoverageReport]) -> str:
    """End-of-run data-quality posture, combining every gate that ran. Printed
    on every pipeline run (success or failure) so the data-quality state is
    never silent."""
    results = [r for rep in reports for r in rep.results]
    blocking = [r for r in results if r.severity == BLOCKING]
    advisory = [r for r in results if r.severity == ADVISORY]
    bar = "=" * 78
    lines = [bar, "RUN DATA-QUALITY SUMMARY", bar]

    b_ok = sum(1 for r in blocking if r.ok)
    lines.append(f"BLOCKING : {b_ok}/{len(blocking)} sources OK")

    a_full = [r for r in advisory if r.status == OK]
    a_gap = [r for r in advisory if r.status != OK]
    if advisory:
        gap_names = ", ".join(r.source for r in a_gap) or "none"
        lines.append(
            f"ADVISORY : {len(a_full)}/{len(advisory)} fully covered, "
            f"{len(a_gap)} with gaps ({gap_names})"
        )
        for r in a_gap:
            if r.n_expected is not None and r.n_present is not None:
                miss = r.n_expected - r.n_present
                lines.append(
                    f"           └ {r.source}: {miss} funds excluded "
                    f"({r.n_present}/{r.n_expected} covered) — {r.status}"
                )
            else:
                lines.append(f"           └ {r.source}: {r.status} — {r.detail}")
    else:
        lines.append("ADVISORY : (not evaluated — Phase-2 ingest skipped)")
    return "\n".join(lines)
