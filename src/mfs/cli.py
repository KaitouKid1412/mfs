"""mfs CLI — Typer entry point."""

from __future__ import annotations

from datetime import date

import typer

from mfs import paths
from mfs.utils.logging import configure_logging, get_logger

app = typer.Typer(help="Indian mutual fund evaluation pipeline.")
ingest_app = typer.Typer(help="Data ingestion subcommands.")
build_app = typer.Typer(help="Build derived datasets.")
compute_app = typer.Typer(help="Compute metric snapshots.")
app.add_typer(ingest_app, name="ingest")
app.add_typer(build_app, name="build")
app.add_typer(compute_app, name="compute")

log = get_logger("mfs.cli")


def _parse_date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


@ingest_app.command("navs")
def ingest_navs(
    backfill: bool = typer.Option(False, help="Fetch full history from configured start"),
    since: str | None = typer.Option(None, help="Fetch incremental since YYYY-MM-DD"),
):
    """Ingest AMFI daily NAVs."""
    configure_logging()
    from mfs.ingest import amfi_nav

    if backfill:
        n, years = amfi_nav.ingest_backfill()
    elif since:
        n, years = amfi_nav.ingest_since(date.fromisoformat(since))
    else:
        n, years = amfi_nav.ingest_today()
    typer.echo(f"NAVs ingested: rows={n}, years_touched={years}")


@ingest_app.command("benchmarks")
def ingest_benchmarks(
    tickers: list[str] = typer.Option(None, help="Specific tickers to fetch; default = all"),
    since: str | None = typer.Option(None, help="Fetch from YYYY-MM-DD onwards"),
):
    """Ingest NSE TRI benchmark series."""
    configure_logging()
    from mfs.ingest import benchmarks

    start = _parse_date(since)
    if tickers:
        results = {t: benchmarks.ingest_ticker(t, start=start) for t in tickers}
    else:
        results = benchmarks.ingest_all_known(start=start)
    typer.echo(f"Benchmarks ingested: {results}")


@ingest_app.command("tbill")
def ingest_tbill(
    backfill: bool = typer.Option(False, help="Re-walk full history (otherwise incremental)."),
    since: str | None = typer.Option(None, help="Build curated series starting at YYYY-MM-DD."),
    years: float = typer.Option(5.0, help="How many years of RBI press releases to crawl."),
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


@build_app.command("scheme-master")
def build_scheme_master():
    """Build the canonical scheme_master from today's AMFI snapshot + NAV history."""
    configure_logging()
    from mfs.master import scheme_master

    df = scheme_master.build()
    typer.echo(f"scheme_master rows: {df.height}")


@compute_app.command("metrics")
def compute_metrics(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default = today"),
    scheme: str | None = typer.Option(None, help="Compute one scheme only (debug)"),
):
    """Compute the metric snapshot for `as_of`."""
    configure_logging()
    from mfs.compute import orchestrator

    d = date.fromisoformat(as_of) if as_of else date.today()
    df = orchestrator.run(as_of=d, only_scheme=scheme)
    typer.echo(f"computed_metrics partition rows: {df.height}")


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


@app.command("status")
def status_cmd():
    """Print which partitions exist."""
    configure_logging()
    from mfs import paths

    def _list_partitions(root):
        if not root.exists():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir())

    typer.echo(f"nav_daily years:         {_list_partitions(paths.nav_daily_dataset())}")
    typer.echo(f"benchmark_daily tickers: {_list_partitions(paths.benchmark_daily_dataset())}")
    typer.echo(f"risk_free_daily exists:  {paths.risk_free_daily_file().exists()}")
    typer.echo(f"scheme_master exists:    {paths.scheme_master_file().exists()}")
    typer.echo(f"computed_metrics dates:  {_list_partitions(paths.computed_metrics_dataset())}")


@app.command("validate")
def validate_cmd():
    """Data quality report: TRI sanity (Nifty 50 CAGR > 12% since 2010), NAV gaps."""
    configure_logging()
    import polars as pl

    from mfs import paths
    from mfs.io.parquet import read_dataset

    bench = read_dataset(paths.benchmark_daily_dataset())
    if bench.is_empty():
        typer.echo("WARN: no benchmark data ingested")
    else:
        nifty = bench.filter(pl.col("ticker") == "NIFTY 50 TRI").sort("date")
        if not nifty.is_empty():
            cutoff = pl.date(2010, 1, 1)
            sub = nifty.filter(pl.col("date") >= cutoff)
            if sub.height >= 2:
                start_close = sub["close"][0]
                end_close = sub["close"][-1]
                years = (sub["date"][-1] - sub["date"][0]).days / 365.25
                cagr = (end_close / start_close) ** (1 / years) - 1
                status = "OK" if cagr > 0.10 else "SUSPECT (PR not TRI?)"
                typer.echo(f"NIFTY 50 TRI CAGR since 2010: {cagr*100:.2f}%  [{status}]")
            else:
                typer.echo("WARN: insufficient Nifty 50 TRI history for sanity check")

    nav = read_dataset(paths.nav_daily_dataset())
    if nav.is_empty():
        typer.echo("WARN: no NAV data ingested")
    else:
        n_schemes = nav["scheme_code"].n_unique()
        typer.echo(f"NAV daily: {nav.height} rows across {n_schemes} schemes")


@app.command("pipeline")
def pipeline_run_all(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default = today"),
    allow_fallback: bool = typer.Option(
        False, help="Emit synthetic 6.5% T-bill series if real data fetch fails."
    ),
):
    """Run ingest → freshness check → build → compute → rank end-to-end.

    Halts at the first stage that fails. No partial / stale data ever reaches
    compute or rank.
    """
    configure_logging()
    from mfs.compute import orchestrator
    from mfs.errors import PipelineError
    from mfs.freshness import check_freshness
    from mfs.ingest import amfi_nav, benchmarks, fbil_tbill
    from mfs.master import scheme_master
    from mfs.rank import shortlist

    d = date.fromisoformat(as_of) if as_of else date.today()

    def _stage(name: str, fn):
        typer.echo(f"[pipeline] {name} ...")
        try:
            return fn()
        except PipelineError as e:
            typer.echo(f"[pipeline] FAIL at {name}:\n{e}", err=True)
            raise typer.Exit(code=2) from e

    _stage("ingest navs (today)", amfi_nav.ingest_today)
    _stage("ingest benchmarks", benchmarks.ingest_all_known)
    _stage("ingest tbill", lambda: fbil_tbill.ingest(allow_fallback=allow_fallback))
    _stage("build scheme-master", scheme_master.build)
    _stage("freshness check", lambda: check_freshness(as_of=d, raise_on_fail=True))
    _stage("compute metrics", lambda: orchestrator.run(as_of=d))
    out = _stage("rank", lambda: shortlist.rank(as_of=d))
    typer.echo(f"pipeline done. {len(out)} category files in data/output/shortlist/{d.isoformat()}/")


if __name__ == "__main__":
    app()
