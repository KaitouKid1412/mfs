"""mfs CLI — Typer entry point."""

from __future__ import annotations

from datetime import date

import typer

from mfs import paths
from mfs.db.queries import TRI_SANITY_MIN_CAGR
from mfs.utils.logging import configure_logging, get_logger

app = typer.Typer(help="Indian mutual fund evaluation pipeline.")
ingest_app = typer.Typer(help="Data ingestion subcommands.")
build_app = typer.Typer(help="Build derived datasets.")
compute_app = typer.Typer(help="Compute metric snapshots.")
db_app = typer.Typer(help="Postgres data layer (init / migrate / verify).")
app.add_typer(ingest_app, name="ingest")
app.add_typer(build_app, name="build")
app.add_typer(compute_app, name="compute")
app.add_typer(db_app, name="db")

log = get_logger("mfs.cli")


def _parse_date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


@ingest_app.command("navs")
def ingest_navs(
    backfill: bool = typer.Option(False, help="Fetch full history from configured start"),
    since: str | None = typer.Option(None, help="Fetch incremental since YYYY-MM-DD"),
    record_missing: bool = typer.Option(
        False,
        "--record-missing",
        help="On --backfill, log failed windows instead of raising. Use only for "
        "deep historical backfills (e.g. 2013-onwards) where AMFI may refuse old windows.",
    ),
):
    """Ingest AMFI daily NAVs."""
    configure_logging()
    from mfs.ingest import amfi_nav

    if backfill:
        n, years = amfi_nav.ingest_backfill(fail_on_missing=not record_missing)
    elif since:
        n, years = amfi_nav.ingest_since(date.fromisoformat(since))
    else:
        n, years = amfi_nav.ingest_today()
    typer.echo(f"NAVs ingested: rows={n}, years_touched={years}")


@ingest_app.command("benchmarks")
def ingest_benchmarks(
    tickers: list[str] = typer.Option(None, help="Specific tickers to fetch; default = all"),
    since: str | None = typer.Option(None, help="Fetch from YYYY-MM-DD onwards"),
    full: bool = typer.Option(
        False, "--full",
        help="Re-fetch full history (default: incremental from each ticker's "
        "latest stored date minus a restatement tail).",
    ),
    skip_hybrid: bool = typer.Option(
        False, help="Skip the synthetic hybrid TRI construction step."
    ),
):
    """Ingest NSE TRI benchmark series, then synthesize hybrid TRIs from components."""
    configure_logging()
    from mfs.ingest import benchmarks, synthetic_hybrid

    start = _parse_date(since)
    if tickers:
        results = {t: benchmarks.ingest_ticker(t, start=start, full=full) for t in tickers}
    else:
        results = benchmarks.ingest_all_known(start=start, full=full)
    typer.echo(f"Benchmarks ingested: {results}")
    if not skip_hybrid:
        try:
            hybrid_results = synthetic_hybrid.synthesize_all(start=start)
            typer.echo(f"Synthetic hybrid TRIs built: {hybrid_results}")
        except RuntimeError as e:
            typer.echo(
                f"Hybrid synthesis skipped: {e}", err=True
            )


@ingest_app.command("tbill")
def ingest_tbill(
    backfill: bool = typer.Option(False, help="Re-walk full history (otherwise incremental)."),
    since: str | None = typer.Option(None, help="Build curated series starting at YYYY-MM-DD."),
    years: float = typer.Option(13.5, help="How many years of RBI press releases to crawl."),
    start_prid: int | None = typer.Option(None, help="Explicit lower bound on prid (debug)."),
    end_prid: int | None = typer.Option(None, help="Explicit upper bound on prid (debug)."),
    allow_fallback: bool = typer.Option(
        False, help="Emit the synthetic 6.5% flat series if real data is unavailable."
    ),
):
    """Ingest 91-day T-bill yields from RBI press releases → risk-free rate series."""
    configure_logging()
    from mfs.ingest import fbil_tbill

    start = _parse_date(since)
    if backfill:
        # Reset incremental state so the walk uses the `years` lookback fully.
        state_file = paths.rbi_tbill_state_file()
        if state_file.exists():
            state_file.unlink()
    try:
        n = fbil_tbill.ingest(
            start=start,
            allow_fallback=allow_fallback,
            years=years,
            start_prid=start_prid,
            end_prid=end_prid,
        )
    except fbil_tbill.NoRealRiskFreeDataError as e:
        typer.echo(f"ERROR: {e}", err=True)
        raise typer.Exit(code=2) from e
    typer.echo(f"Risk-free series: rows={n}")


@ingest_app.command("ter")
def ingest_ter():
    """Ingest user-provided TER CSVs (data/raw/amfi_ter/manual/*.csv)."""
    configure_logging()
    from mfs.ingest import amfi_ter

    n = amfi_ter.ingest_from_manual_csvs()
    typer.echo(f"TER rows ingested: {n}")


@ingest_app.command("amfi-aum")
def ingest_amfi_aum(
    quarter: str = typer.Option(
        ..., "--quarter",
        help="Quarter label like 'Q4-2026' (Jan-Mar 2026) or 'Q1-2025' (Apr-Jun 2025).",
    ),
):
    """Ingest AMFI quarterly per-scheme Average AUM.

    Single GET against the AMFI schemewise endpoint covers all SEBI-registered
    DIRECT+GROWTH schemes in one shot — replaces per-AMC factsheet AUM
    extraction. Stamps rows with source_amc='amfi_aaum' and as_of_month set
    to the first day of the quarter's last month.
    """
    configure_logging()
    from mfs.ingest import amfi_aum

    result = amfi_aum.ingest_quarter(quarter)
    typer.echo(
        f"amfi_aum {quarter} (as_of={result['as_of_month']}): "
        f"parsed={result['rows_parsed']} written={result['rows_written']}"
    )


@ingest_app.command("bhavcopy")
def ingest_bhavcopy(
    date_str: str | None = typer.Option(
        None, "--date", help="YYYY-MM-DD; ingest only this single day."
    ),
    days: int = typer.Option(
        75, "--days", help="Walk back N calendar days (default 75 — covers a 60-day "
        "median window plus holiday slack)."
    ),
):
    """Ingest NSE daily bhavcopy CSVs into stock_adv_daily (Phase 2.3.B).

    With no flags, walks back ~75 days and ingests each trading day. Cached
    CSVs on disk are reused. Symbol→ISIN mapping is refreshed via EQUITY_L.csv.
    """
    configure_logging()
    from mfs.ingest import bhavcopy

    if date_str:
        result = bhavcopy.fetch_one(date.fromisoformat(date_str))
        typer.echo(f"bhavcopy {date_str}: rows={result}")
    else:
        result = bhavcopy.ingest_recent(n_days=days)
        typer.echo(
            f"bhavcopy: days_with_data={result['days_with_data']} "
            f"total_rows={result['total_rows']}"
        )


@ingest_app.command("constituents")
def ingest_constituents(
    ticker: str | None = typer.Option(
        None, help="Run only this benchmark ticker (e.g. 'NIFTY 100 TRI')."
    ),
):
    """Ingest benchmark index constituent weights from manual CSVs.

    Reads `data/raw/index_constituents/manual/<ticker_slug>/<YYYY-MM>.csv`
    files and upserts into `index_constituents_monthly`. NSE doesn't publish
    constituent weights as a clean CSV; manual input is the primary path.
    """
    configure_logging()
    from mfs.ingest import constituents

    if ticker:
        result = constituents.run_for_ticker(ticker)
        typer.echo(
            f"{ticker}: rows_written={result['rows_written']} "
            f"months={result.get('n_months', 0)}"
        )
    else:
        results = constituents.run_all()
        if not results:
            typer.echo("No manual constituent CSVs found.")
            return
        for t, r in results.items():
            typer.echo(
                f"{t}: rows_written={r['rows_written']} months={r['n_months']}"
            )


@ingest_app.command("managers")
def ingest_managers(
    amc: str | None = typer.Option(None, help="Run only this AMC slug (e.g. 'hdfc')."),
    ym: str | None = typer.Option(None, help="Data month YYYY-MM; default = last month."),
    pdf_path: str | None = typer.Option(
        None, "--pdf-path",
        help="Local PDF override — skip download and parse this file (requires --amc).",
    ),
    list_adapters: bool = typer.Option(
        False, "--list", help="List registered AMC adapters and exit."
    ),
):
    """Ingest holdings + PTR signals from AMC factsheet PDFs.

    The CLI name remains ``ingest managers`` for backward compatibility, but
    manager-tenure extraction was retired (manual verification for Stage 2
    survivors) and AUM extraction was removed in favour of AMFI's quarterly
    AAUM endpoint — these adapters now produce holdings (factsheet path, no
    ISIN) and PTR only.
    """
    configure_logging()
    from pathlib import Path
    from mfs.ingest import managers
    from mfs.ingest.managers._registry import registered_adapters

    if list_adapters:
        for slug in registered_adapters():
            typer.echo(slug)
        return

    if pdf_path is not None and amc is None:
        typer.echo("ERROR: --pdf-path requires --amc to identify the adapter.", err=True)
        raise typer.Exit(2)

    if amc:
        result = managers.run_for_amc(
            amc, ym=ym, pdf_path=Path(pdf_path) if pdf_path else None,
        )
        typer.echo(
            f"{amc}: holdings={result['rows_written_holdings']} "
            f"ptr={result['rows_written_ptr']}"
        )
    else:
        results = managers.run_all(ym=ym)
        for slug, r in results.items():
            typer.echo(
                f"{slug}: holdings={r['rows_written_holdings']} "
                f"ptr={r['rows_written_ptr']}"
            )


@ingest_app.command("holdings")
def ingest_holdings(
    amc: str | None = typer.Option(None, help="Run only this AMC slug (e.g. 'hdfc')."),
    ym: str | None = typer.Option(None, help="Data month YYYY-MM; default = last month."),
    list_adapters: bool = typer.Option(
        False, "--list", help="List registered holdings adapters and exit."
    ),
):
    """Ingest per-scheme monthly portfolio Excels with ISINs (Phase 3.C).

    Unlike the factsheet-based holdings path inside `ingest managers`, these
    rows carry ISIN (NOT NULL at the row level) — which unblocks AUM Impact
    Cost computation and tightens Active Share security matching.
    """
    configure_logging()
    from mfs.ingest import holdings
    from mfs.ingest.holdings._registry import registered_adapters

    if list_adapters:
        for slug in registered_adapters():
            typer.echo(slug)
        return

    if amc:
        result = holdings.run_for_amc(amc, ym=ym)
        typer.echo(
            f"{amc}: rows_written={result['rows_written']} "
            f"schemes_discovered={result['n_schemes_discovered']} "
            f"schemes_parsed={result['n_schemes_parsed']} "
            f"records={result['n_records']} "
            f"unmatched={len(result['unmatched_schemes'])}"
        )
        if result["unmatched_schemes"]:
            typer.echo("Unmatched scheme names (no candidate ≥ threshold):")
            for name in result["unmatched_schemes"]:
                typer.echo(f"  - {name!r}")
    else:
        results = holdings.run_all(ym=ym)
        for slug, r in results.items():
            typer.echo(
                f"{slug}: rows_written={r['rows_written']} "
                f"schemes_discovered={r['n_schemes_discovered']} "
                f"schemes_parsed={r['n_schemes_parsed']} "
                f"records={r['n_records']}"
            )


@build_app.command("scheme-master")
def build_scheme_master():
    """Build the canonical scheme_master from today's AMFI snapshot + NAV history."""
    configure_logging()
    from mfs.master import scheme_master

    df = scheme_master.build()
    typer.echo(f"scheme_master rows: {df.height}")


@compute_app.command("phase1")
def compute_phase1(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default = today"),
    scheme: str | None = typer.Option(None, help="Compute one scheme only (debug)"),
):
    """Compute Phase 1 metrics (Performance & Consistency) for every
    eligible Direct+Growth scheme."""
    configure_logging()
    from mfs.compute import orchestrator

    d = date.fromisoformat(as_of) if as_of else date.today()
    df = orchestrator.run_phase1(as_of=d, only_scheme=scheme)
    typer.echo(f"phase1 rows written: {df.height}")


@compute_app.command("phase2")
def compute_phase2(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default = today"),
    scheme: str | None = typer.Option(
        None, help="Compute one scheme only (otherwise: every row in the as_of partition)."
    ),
):
    """Compute Phase 2 metrics (style drift / Active Share / PTR / AUM
    impact) by UPDATEing the existing Phase 1 rows.

    Without ``--scheme``, runs over every Phase 1 row in the as_of partition.
    The pipeline (``mfs pipeline``) and ``rank-deep`` restrict Phase 2 to
    just the Stage 1 top-20 pool for efficiency."""
    configure_logging()
    from mfs.compute import orchestrator

    d = date.fromisoformat(as_of) if as_of else date.today()
    codes = [scheme] if scheme else None
    df = orchestrator.run_phase2(as_of=d, scheme_codes=codes)
    typer.echo(f"phase2 rows updated: {df.height}")


@compute_app.command("metrics")
def compute_metrics(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default = today"),
    scheme: str | None = typer.Option(None, help="Compute one scheme only (debug)"),
):
    """Compute Phase 1 + Phase 2 metrics for every eligible scheme.

    Back-compat wrapper for ``compute phase1`` + ``compute phase2``. The
    staged pipeline (``mfs pipeline``) prefers the split commands so Phase 2
    only runs on the Stage 1 narrowed set."""
    configure_logging()
    from mfs.compute import orchestrator

    d = date.fromisoformat(as_of) if as_of else date.today()
    df = orchestrator.run(as_of=d, only_scheme=scheme)
    typer.echo(f"computed_metrics partition rows: {df.height}")


@app.command("audit-scheme")
def audit_scheme(
    scheme_code: str = typer.Argument(..., help="AMFI scheme code"),
    benchmark: str = typer.Option(
        "NIFTY 50 TRI", help="Benchmark ticker to regress against"
    ),
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default = today"),
    step: str = typer.Option("1w", help="Rolling step (1w or 1d)"),
):
    """Compute metrics for one (scheme, benchmark) pair on the fly.

    Bypasses scheme_master eligibility — useful for sanity checks on index
    funds that have no canonical_category and would otherwise be skipped.

    Output is printed to stdout; nothing is written to computed_metrics.
    """
    configure_logging()
    from mfs.compute import orchestrator
    from mfs.db import queries as q

    d = _parse_date(as_of) or date.today()

    sm = q.scheme_master()
    if sm.is_empty():
        typer.echo("scheme_master is empty", err=True)
        raise typer.Exit(1)
    match = sm.filter(__import__("polars").col("scheme_code") == scheme_code)
    name = match["scheme_name"][0] if not match.is_empty() else "(not in scheme_master)"
    cat = match["canonical_category"][0] if not match.is_empty() else None

    row = orchestrator.compute_for_scheme(
        scheme_code=scheme_code,
        benchmark_ticker=benchmark,
        canonical_category=cat,
        as_of=d,
        step=step,
    )

    def _pct(v):
        return f"{v * 100:.2f}%" if v is not None else "—"

    def _num(v, digits=3):
        return f"{v:.{digits}f}" if v is not None else "—"

    typer.echo("")
    typer.echo(f"  scheme_code:       {scheme_code}")
    typer.echo(f"  scheme_name:       {name}")
    typer.echo(f"  category:          {cat or '—'}")
    typer.echo(f"  benchmark:         {benchmark}")
    typer.echo(f"  as_of:             {d.isoformat()}")
    typer.echo(f"  data_quality_flag: {row['data_quality_flag']}")
    typer.echo("")
    typer.echo(f"  ret_3y_median:       {_pct(row['ret_3y_median'])}")
    typer.echo(f"  ret_3y_p25:          {_pct(row['ret_3y_p25'])}")
    typer.echo(f"  ret_5y_median:       {_pct(row['ret_5y_median'])}")
    typer.echo(f"  ret_5y_p25:          {_pct(row['ret_5y_p25'])}")
    typer.echo("")
    typer.echo(f"  alpha_3y_annualized: {_pct(row['alpha_3y_annualized'])}")
    typer.echo(f"  alpha_3y_tstat:      {_num(row['alpha_3y_tstat'], 2)}")
    typer.echo(f"  beta_3y:             {_num(row['beta_3y'])}")
    typer.echo(f"  r_squared_3y:        {_num(row['r_squared_3y'])}")
    typer.echo("")
    typer.echo(f"  sortino_3y:          {_num(row['sortino_3y'])}")
    typer.echo(f"  info_ratio_3y:       {_num(row['info_ratio_3y'])}")
    typer.echo(f"  capture_up:          {_num(row['capture_up'])}")
    typer.echo(f"  capture_down:        {_num(row['capture_down'])}")
    typer.echo(f"  capture_efficiency:  {_num(row['capture_efficiency'])}")


@app.command("rank")
def rank_cmd(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default = latest computed"),
    category: str | None = typer.Option(None, help="Restrict to one canonical_category"),
    top_n: int | None = typer.Option(None, help="Override top-N per category"),
    skip_filters: bool = typer.Option(
        False,
        "--skip-filters",
        help="Bypass hard filters (debug/sanity-check only).",
    ),
):
    """Apply hard filters + z-score + composite, write shortlist CSVs."""
    configure_logging()
    from mfs.rank import shortlist

    d = _parse_date(as_of)
    out = shortlist.rank(as_of=d, category=category, top_n=top_n, skip_filters=skip_filters)
    typer.echo(f"Wrote {len(out)} category shortlist file(s):")
    for cat, p in out.items():
        typer.echo(f"  {cat:30s} -> {p}")


@app.command("rank-deep")
def rank_deep_cmd(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default = latest computed"),
    pool_size: int = typer.Option(
        20, help="Top-N per category considered for Stage 2 (default 20)."
    ),
    final_size: int = typer.Option(
        5, help="Top-N per category in Stage 2 survivors output (default 5)."
    ),
    overlap_threshold: float = typer.Option(
        30.0,
        help="Stage 3 overlap threshold percent (default 30.0). Pairs strictly "
        "above this are dropped in iteration.",
    ),
    skip_phase2_compute: bool = typer.Option(
        False, "--skip-phase2-compute",
        help="Skip running Phase 2 metric compute on the candidate pool. "
        "Use when Phase 2 columns are already filled in computed_metrics.",
    ),
):
    """Run the staged pipeline: Stage 1 Phase-1 ranking + Stage 2 Phase-2
    re-rank + Stage 3 iterative overlap drop.

    Stage 1 outputs land in ``<as_of>/stage1/`` (per-category CSVs + parquet,
    plus ``mf_report``). Stage 2 in ``<as_of>/stage2/`` (re-ranked survivors,
    dropped, coverage, mf_report). Stage 3 in ``<as_of>/stage3/`` (final
    picks per category, dropped, overlap_pairs, mf_report).
    """
    configure_logging()
    from mfs.rank import shortlist

    d = _parse_date(as_of)
    result = shortlist.rank_deep(
        as_of=d,
        pool_size=pool_size,
        final_size=final_size,
        overlap_threshold_pct=overlap_threshold,
        skip_phase2_compute=skip_phase2_compute,
    )
    typer.echo(f"as_of: {result['as_of']}")
    typer.echo(f"out_dir: {result['out_dir']}")
    typer.echo(
        f"stage 1: {len(result['stage1'])} category file(s) at {result['out_dir']}/stage1/"
    )
    typer.echo(
        f"stage 2: survivors={result['stage2']['n_survivors']} "
        f"dropped={result['stage2']['n_dropped']} "
        f"(coverage: {result['stage2']['coverage_file']})"
    )
    typer.echo(
        f"stage 3: final={result['stage3']['n_final']} "
        f"dropped={result['stage3']['n_dropped']} "
        f"@ overlap > {overlap_threshold}% "
        f"(dropped: {result['stage3']['dropped_file']})"
    )


@app.command("status")
def status_cmd():
    """Print row counts + date bounds per table."""
    configure_logging()
    from mfs.db import queries as q
    from mfs.db.connection import get_dsn

    typer.echo(f"DSN: {get_dsn()}")
    for name in q.STATUS_TABLES:
        try:
            n, mn, mx = q.table_bounds(name)
            typer.echo(f"  {name:18s}: {n:>10} rows  [{mn} .. {mx}]")
        except Exception as e:  # noqa: BLE001
            typer.echo(f"  {name:18s}: <error: {e}>")
    # Output is files, not parquet — show what's on disk
    n_categories = 0
    out = paths.output_dir() / "shortlist"
    if out.exists():
        latest_run = max((p for p in out.iterdir() if p.is_dir()), default=None, key=lambda p: p.name)
        if latest_run:
            # Stage 1 outputs land under <as_of>/stage1/; older runs (pre Phase 3)
            # wrote to the top level. Try stage1/ first, fall back to top level.
            stage1_dir = latest_run / "stage1"
            search_dir = stage1_dir if stage1_dir.exists() else latest_run
            n_categories = sum(
                1 for f in search_dir.glob("*.csv")
                if not f.name.startswith("mf_report")
            )
            typer.echo(f"  shortlist (latest): {latest_run.name}, {n_categories} category CSVs")


@db_app.command("init")
def db_init():
    """Create the target Postgres DB if missing, then apply schema."""
    configure_logging()
    from mfs.db import init as db_init_mod
    from mfs.db.connection import get_dsn

    typer.echo(f"DSN: {get_dsn()}")
    db_init_mod.init()
    typer.echo("Schema applied.")


@db_app.command("migrate")
def db_migrate():
    """One-shot load of parquet datasets into Postgres. Idempotent."""
    configure_logging()
    from mfs.db.migrate import migrate_all

    results = migrate_all()
    typer.echo("Migration totals (rows touched per table):")
    for k, v in results.items():
        typer.echo(f"  {k:20s}: {v}")


@db_app.command("verify")
def db_verify():
    """Cross-check parquet vs Postgres row counts and date bounds."""
    configure_logging()
    from mfs.db.verify import verify_all

    rows = verify_all()
    typer.echo("Parquet ↔ Postgres verification:")
    for r in rows:
        typer.echo(str(r))
    all_ok = all(r.ok for r in rows)
    if not all_ok:
        typer.echo("\nFAIL: at least one table mismatches. Do NOT delete parquet.", err=True)
        raise typer.Exit(code=2)
    typer.echo("\nAll tables match. Safe to delete parquet curated/metrics directories.")


@db_app.command("coverage")
def db_coverage(
    since: str = typer.Option("2013-01-01", help="Coverage window start (YYYY-MM-DD)."),
    nav_show_top: int = typer.Option(
        20, help="In NAV section, list this many schemes with the worst coverage."
    ),
    nav_min_inception_year: int = typer.Option(
        2013, help="Only count schemes whose inception was in this year or later."
    ),
):
    """Verify trading-day coverage of NAV / TRI / risk-free since `--since`.

    Trading-day reference = NIFTY 50 TRI dates since `--since`. For each
    benchmark ticker: how many trading days have no row in benchmark_daily? For
    risk-free: how many trading days have no rate (after daily forward-fill)?
    For NAV: per active Direct+Growth scheme in a rankable category, how many
    trading days between inception and last_seen are missing a NAV?
    """
    configure_logging()
    from datetime import date as _date
    from mfs.db import queries as q

    since_d = _date.fromisoformat(since)

    # Reference trading calendar (NIFTY 50 TRI dates since `since`)
    cal_count, cal_min, cal_max = q.trading_calendar_bounds(since_d)
    typer.echo(
        f"Reference calendar: {cal_count} NIFTY 50 TRI trading days, "
        f"{cal_min} → {cal_max}"
    )
    typer.echo("=" * 80)

    # --- Benchmarks ---
    typer.echo("\n[BENCHMARKS] Days missing per ticker vs trading calendar:")
    for ticker, exp, actual, miss, first, last in q.benchmark_gap_report(since_d):
        flag = "OK " if miss == 0 else "GAP"
        typer.echo(
            f"  [{flag}] {ticker:38s}  expected={exp}  actual={actual}  "
            f"missing={miss}  ({first} → {last})"
        )

    # --- Risk-free ---
    typer.echo("\n[RISK-FREE] Trading-day coverage:")
    rf = q.risk_free_gap_report(since_d)
    if rf is None:
        typer.echo("  No risk-free data.")
    else:
        typer.echo(
            f"  Earliest observation: {rf.rf_min}, latest: {rf.rf_max}"
        )
        typer.echo(
            f"  Trading days {since_d}..{rf.rf_min - _date.resolution} (before rf): "
            f"{rf.n_pre} unfilled"
        )
        typer.echo(
            f"  Trading days {rf.rf_min}..today with no rf row "
            f"(should be 0 — forward-fill should cover all): "
            f"{len(rf.missing_dates)}"
        )
        for d in rf.missing_dates[:10]:
            typer.echo(f"    {d}")

    # --- NAVs (per scheme, only rankable Direct+Growth) ---
    typer.echo(
        f"\n[NAV] Active rankable Direct+Growth schemes "
        f"(inception ≥ {nav_min_inception_year}):"
    )
    # Use the rankable categories list from thresholds yaml
    from mfs.config import get_thresholds
    rankable = get_thresholds().get("rankable_categories", [])
    if not rankable:
        typer.echo("  (no rankable_categories configured)")
    else:
        results = q.nav_coverage_report(
            since_d, list(rankable), nav_min_inception_year
        )
        total = len(results)
        perfect = sum(1 for r in results if r[6] == 0)
        within_5 = sum(1 for r in results if r[6] <= 5)
        within_30 = sum(1 for r in results if r[6] <= 30)
        typer.echo(f"  Total eligible schemes: {total}")
        typer.echo(f"    100% coverage (0 days missing):  {perfect}")
        typer.echo(f"    ≤5 days missing:                  {within_5}")
        typer.echo(f"    ≤30 days missing:                 {within_30}")
        typer.echo(f"    >30 days missing:                 {total - within_30}")
        typer.echo(f"\n  Top {nav_show_top} schemes with the most missing trading days:")
        for r in results[:nav_show_top]:
            code, name, incep, last, exp, act, miss = r
            typer.echo(
                f"    {code:>8}  {(name or '')[:60]:60s}  "
                f"incep={incep}  last={last}  "
                f"expected={exp:5d}  actual={act:5d}  missing={miss}"
            )


@db_app.command("status")
def db_status():
    """Show current DB name and row counts per table."""
    configure_logging()
    from mfs.db import queries as q
    from mfs.db.connection import get_dsn

    typer.echo(f"DSN: {get_dsn()}")
    for tbl in q.STATUS_TABLES:
        try:
            n = q.table_count(tbl)
            typer.echo(f"  {tbl:20s}: {n} rows")
        except Exception as e:  # noqa: BLE001
            typer.echo(f"  {tbl:20s}: <error: {e}>")


@app.command("coverage")
def coverage_cmd(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default = today"),
    gate: str = typer.Option(
        "all", help="Which gate to evaluate: 'A' (blocking), 'B' (advisory), or 'all'."
    ),
):
    """Evaluate the data-coverage contracts and print the per-source report.

    Answers, for every ingested source, the six coverage questions (need-from /
    have-from / have-till / fresh-by / expected-vs-actual / gap+fix). This is the
    same machinery the pipeline runs at Gate A / Gate B, runnable standalone for
    diagnostics. Read-only — never ingests or computes.

    Exits 2 if a BLOCKING contract fails (so it can be used as a CI/cron check).
    """
    configure_logging()
    from mfs import coverage

    d = date.fromisoformat(as_of) if as_of else date.today()
    gates = ["A", "B"] if gate == "all" else [gate.upper()]
    reports = []
    for g in gates:
        report = coverage.run_gate(g, as_of=d, raise_on_block=False)
        typer.echo(coverage.render(report))
        reports.append(report)
    typer.echo(coverage.render_summary(reports))
    if any(rep.blocking_failures for rep in reports):
        raise typer.Exit(code=2)


@app.command("missing-data")
def missing_data_cmd(
    since: str = typer.Option(
        "2013-01-01", help="Inventory start date (YYYY-MM-DD)."
    ),
    nav_per_scheme_top: int = typer.Option(
        20, help="In the NAV section, list at most this many worst-coverage schemes."
    ),
):
    """Inventory of missing data across NAV / TRI / risk-free, since `--since`.

    Counts expected vs observed business-day samples for each series and prints
    a concise report. Designed to be run after a backfill to surface exactly
    where the gaps are.
    """
    configure_logging()
    from datetime import date as _date, timedelta
    import polars as pl

    from mfs import paths
    from mfs.db import queries as q

    since_d = _date.fromisoformat(since)
    today = _date.today()

    def _weekday_count(start: _date, end: _date) -> int:
        days, cur = 0, start
        while cur <= end:
            if cur.weekday() < 5:
                days += 1
            cur += timedelta(days=1)
        return days

    expected_bdays = _weekday_count(since_d, today)
    typer.echo(
        f"Missing-data inventory  ({since_d} → {today}, expected ~{expected_bdays} business days)"
    )
    typer.echo("=" * 78)

    # --- Benchmarks ---
    typer.echo("\n[BENCHMARKS — TRI series]")
    rows = q.benchmark_inventory(since_d)
    if not rows:
        typer.echo("  (no benchmark data)")
    for ticker, n, first, last in rows:
        missing = expected_bdays - n
        typer.echo(
            f"  {ticker:38s}  rows={n:5d}  first={first}  last={last}  "
            f"~bdays missing={missing if missing > 0 else 0}"
        )

    # --- Risk-free ---
    typer.echo("\n[RISK-FREE — 91-day T-bill cut-off]")
    scraped = paths.raw_dir() / "fbil_tbill" / "rbi_scraped_91d_tbill.csv"
    if scraped.exists():
        obs = pl.read_csv(scraped, schema_overrides={"date": pl.Date}).filter(
            pl.col("date") >= since_d
        ).sort("date")
        typer.echo(
            f"  Real auction observations since {since_d}: {obs.height} "
            f"(expected ~{(today - since_d).days // 7})"
        )
        dates = obs["date"].to_list()
        gaps = [(a, b, (b - a).days) for a, b in zip(dates, dates[1:]) if (b - a).days > 14]
        typer.echo(f"  Forward-fill stretches >14 days: {len(gaps)}")
        for a, b, d in gaps:
            typer.echo(f"    {a} .. {b}  ({d} days)")
    else:
        typer.echo("  (scraped CSV missing — re-run `mfs ingest tbill`)")

    # --- NAVs ---
    typer.echo(f"\n[NAV — per scheme since {since_d}]")
    per_scheme = q.nav_per_scheme_inventory(since_d)
    if not per_scheme:
        typer.echo("  (no NAV data)")
    else:
        total_schemes = len(per_scheme)
        worst = [(sc, mn, mx, n, exp - n) for sc, mn, mx, n, exp in per_scheme if exp - n > 30]
        typer.echo(f"  Total schemes with any NAV since {since_d}: {total_schemes}")
        typer.echo(f"  Schemes with >30 missing business days within their active window: {len(worst)}")
        worst.sort(key=lambda r: -r[4])
        typer.echo(f"  Top {nav_per_scheme_top} schemes by missing days:")
        for sc, mn, mx, n, miss in worst[:nav_per_scheme_top]:
            typer.echo(
                f"    {sc:>8}  first={mn}  last={mx}  observed={n:5d}  ~missing={miss}"
            )

    # --- AMFI backfill windows that failed ---
    failures_log = paths.raw_dir() / "amfi_nav_history" / "_missing_windows.txt"
    if failures_log.exists():
        typer.echo("\n[AMFI backfill — windows that AMFI refused]")
        for line in failures_log.read_text().strip().splitlines():
            typer.echo(f"  {line}")


@app.command(
    "validate",
    help=(
        "Data quality report: TRI sanity (Nifty 50 TRI CAGR since 2010 must "
        f"exceed {TRI_SANITY_MIN_CAGR:.0%} or the series is flagged SUSPECT), "
        "NAV counts."
    ),
)
def validate_cmd():
    """Thin formatter over ``mfs.db.queries.tri_cagr_sanity`` /
    ``nav_table_counts``; the SUSPECT threshold is
    ``mfs.db.queries.TRI_SANITY_MIN_CAGR`` (also rendered in --help)."""
    configure_logging()
    from datetime import date as _date
    from mfs.db import queries as q

    res = q.tri_cagr_sanity(_date(2010, 1, 1))
    if res is not None:
        status = "OK" if res.ok else "SUSPECT (PR not TRI?)"
        typer.echo(f"NIFTY 50 TRI CAGR since 2010: {res.cagr*100:.2f}%  [{status}]")
    else:
        typer.echo("WARN: no Nifty 50 TRI history since 2010")

    n, n_schemes = q.nav_table_counts()
    typer.echo(f"NAV daily: {n} rows across {n_schemes} schemes")


@app.command("pipeline")
def pipeline_run_all(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default = today"),
    allow_fallback: bool = typer.Option(
        False, help="Emit synthetic 6.5% T-bill series if real data fetch fails."
    ),
    skip_phase2: bool = typer.Option(
        False, "--skip-phase2",
        help="Skip factsheet / holdings / bhavcopy / constituents ingest "
        "stages. Useful for debugging Phase 1 only.",
    ),
    full: bool = typer.Option(
        False, "--full",
        help="Force a from-scratch re-ingest: benchmarks re-fetch full history, "
        "bhavcopy re-walks the full window, factsheets re-parse even if "
        "unchanged. Default is incremental (fetch only the gap; the coverage "
        "gate then verifies the gap closed).",
    ),
):
    """Run the full pipeline end-to-end: ingest → build → compute → rank-deep.

    Phase 1 (NAV, benchmarks, T-bill, scheme-master) is required.
    Factsheet ingest (holdings/PTR/AUM via the per-AMC factsheet adapters)
    plus bhavcopy plus Phase 3.C per-scheme portfolio Excels are required
    for Stage 2/3 of rank-deep to produce non-trivial output. Constituents
    ingest is best-effort — it no-ops cleanly if the manual CSV directory
    is empty.

    Halts at the first REQUIRED stage that fails. No partial / stale data
    ever reaches compute or rank-deep.
    """
    configure_logging()
    from mfs import pipeline
    from mfs.errors import PipelineError

    d = date.fromisoformat(as_of) if as_of else date.today()
    try:
        result = pipeline.run(
            d,
            full=full,
            skip_phase2=skip_phase2,
            allow_fallback=allow_fallback,
            echo=typer.echo,
        )
    except PipelineError as e:
        # pipeline.run already echoed the failure context (FAIL/HALTING/lock
        # message); just translate to the exit code.
        raise typer.Exit(code=2) from e
    typer.echo(f"pipeline done. as_of={result.as_of}")
    typer.echo(
        f"  stage 1: {len(result.stage1_files)} category files at {result.out_dir}/stage1/"
    )
    typer.echo(
        f"  stage 2: survivors={result.stage2_counts['n_survivors']} "
        f"dropped={result.stage2_counts['n_dropped']}"
    )
    typer.echo(
        f"  stage 3: final={result.stage3_counts['n_final']} "
        f"dropped={result.stage3_counts['n_dropped']}"
    )


if __name__ == "__main__":
    app()
