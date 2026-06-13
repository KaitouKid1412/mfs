"""Markdown investor report (E4/E5/E6): ``<run_dir>/REPORT.md``.

Turns a shortlist run directory into a single human-readable report a
retail investor can act on without knowing what a z-score is. Strictly
file-based and DB-free: the primary input is ``stage3/mf_report.csv``
(falling back to ``stage2/mf_report.csv`` with a visible note), plus the
run's exception artifacts (``stage1/excluded.csv``,
``stage3/overlap_breaches.csv`` / ``overlap_pairs.csv``,
``passive_alternative.csv``) and ``stage2/coverage.csv`` for cohort sizes.
The optional ``young_funds`` frame is queried by the CLI
(``mfs.db.queries.young_rankable_schemes``) and passed in, so
``build_report`` itself never opens a DB connection.

Assembly (``load_run``) and rendering (``render_report``) are separate so
an HTML renderer can be added later without touching the file plumbing.
The renderer NEVER raises on schema drift: a missing column or empty cell
renders as '—'.

Static guidance (E6) lives in the module-level ``*_SECTION`` constants and
is rendered into every report: taxes/lock-ins/exit loads, the switch
hysteresis rule of thumb, re-run cadence, the scope boundary, and a
glossary covering every metric column the renderer displays
(``RENDERED_METRIC_COLUMNS`` — pinned by tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import polars as pl

from mfs.utils.logging import get_logger

log = get_logger(__name__)

REPORT_FILENAME = "REPORT.md"

DASH = "—"

STAGE2_FALLBACK_NOTE = (
    "stage3/mf_report.csv was absent from this run — picks below come from "
    "the stage-2 report and carry NO portfolio-overlap flags."
)

#: Cap on overlap pairs rendered in the exception section (the per-pick
#: flags carry the detail; the full list stays in the CSV artifact).
MAX_OVERLAP_PAIRS_RENDERED = 25

#: Display-side fallback (mirrors stage2's disclosure metrics, per E5): when
#: the ``missing_disclosures`` column is absent from an older run's report,
#: a fund is tagged 'partial disclosure' when any of these is null.
DISCLOSURE_FALLBACK_METRICS = (
    "style_drift_3y",
    "active_share_median_1y",
    "ptr_latest",
)

DISCLOSURE_METRIC_LABELS = {
    "active_share_median_1y": "active share (how different the portfolio is "
    "from its index)",
    "style_drift_3y": "style drift (how much the portfolio's style wanders)",
    "ptr_latest": "portfolio turnover (how much the manager trades)",
}

EXCLUSION_REASON_NOTES = {
    "INSUFFICIENT_HISTORY": "less than the required years of price history — "
    "too young to measure, not necessarily bad",
    "STALE_NAV": "price (NAV) has stopped updating — often a suspended or "
    "segregated plan",
}

LIQUIDITY_FLAG_NOTES = {
    "OK": "positions can be unwound quickly",
    "HIGH": "unwinding the least-liquid large positions would take over a "
    "month of trading",
    "SEVERE": "unwinding would take over ~90 trading days — a redemption "
    "rush could hurt remaining investors",
}

# ---------------------------------------------------------------------------
# E6 — static guidance content (documentation, not code).
# ---------------------------------------------------------------------------

DISCLAIMER_BLOCK = """\
> **What this is.** An automatically generated research shortlist built from
> public data (NAVs, index levels, AMC portfolio disclosures). It is **not
> investment advice** and nobody reviewed these picks by hand. Numbers
> describe the *past*; they do not predict the future. Verify the
> details that matter to your money — exit loads, lock-ins, taxation, your
> own risk tolerance — with the AMC's official documents and, if needed, a
> SEBI-registered investment adviser before acting.\
"""

HOW_TO_READ_SECTION = """\
## How to read this report

Every fund below was measured against every other fund in its **category**
(Large Cap funds against Large Cap funds, and so on) — never across
categories. Within each category:

1. **Stage 1** ranks all funds with enough history on returns and
   consistency, and excludes anything it cannot measure cleanly.
2. **Stage 2** takes the leaders and re-ranks them using portfolio-level
   data (how the fund invests, how much it trades, how big it is).
3. **Stage 3** checks the finalists' actual stock holdings against each
   other and flags pairs that own mostly the same things.

The **composite score** is a weighted blend of the fund's metrics, each
standardized within its category (0 = category average, +1 = clearly above
average, -1 = clearly below). It ranks funds *within* a category; comparing
scores across categories is meaningless. Every metric in this report is
explained in plain language in the **Glossary** at the bottom.\
"""

TAXES_SECTION = """\
## Taxes, lock-ins and exit loads

Switching funds is never free. Before selling anything, price these in:

- **ELSS funds carry a 3-year lock-in** — every invested rupee (each SIP
  instalment separately) is locked for three years from its purchase date.
  You cannot switch out of an ELSS position younger than that, period.
- **Long-term capital gains (LTCG):** equity-fund units held over 12 months
  are taxed at **12.5%** on gains above **₹1.25 lakh** per financial year
  (across all your equity sales combined).
- **Short-term capital gains (STCG):** units held 12 months or less are
  taxed at **20%** — selling early is expensive.
- **Exit loads:** most equity funds charge about **1% if you redeem within
  1 year** of purchase; after a year it is typically zero.

Per-scheme exit loads are published by AMFI only inside each scheme's
SID / Scheme Summary Document PDF
(https://www.amfiindia.com/otherdata/scheme-details;
portal.amfiindia.com/spages/SSD_*.pdf) — there is no machine-readable
AMFI/SEBI feed, so this pipeline does not carry them (it never hand-enters
data it cannot verify). **Check the AMC's page for the scheme's current
exit load before redeeming.**\
"""

SWITCHING_SECTION = """\
## When to switch (and when to sit still)

Chasing last quarter's winner usually costs more in taxes and exit loads
than it earns. The hysteresis rule of thumb this pipeline is designed
around:

> Only switch out of a fund you hold when it has fallen **below its
> category's final cutoff (out of the published picks) for
> 2 consecutive runs**, AND the replacement's expected improvement
> plausibly clears the tax + exit-load cost of the move (see the section
> above).

A one-run wobble is noise — funds drift in and out of the bottom of the
list all the time. Two consecutive misses is a trend. Use
`mfs shortlist diff <older-run> <newer-run>` to see exactly what entered,
exited, and moved between two runs, with reasons.\
"""

CADENCE_SECTION = """\
## How often to re-run this

**Quarterly is sufficient.** The slowest-moving inputs (portfolio
holdings, turnover disclosures) update monthly, and the rankings
themselves are sticky — an observed re-run 17 days apart changed 1 fund
out of 34 picks. Re-running weekly produces churn, not insight. A
practical cadence: re-run quarterly, and apply the 2-consecutive-runs
switching rule above to anything you hold.\
"""

SCOPE_SECTION = """\
## What this pipeline does NOT cover

This report ranks **actively managed equity funds plus 3 hybrid categories**
(Aggressive Hybrid, Balanced Advantage, Equity Savings) — the categories
where manager skill is measurable against a market benchmark. That scope
boundary excludes **1,837 of 2,502 active Direct-Growth schemes** by
design: all debt funds, liquid/overnight funds, gold and commodity funds,
international feeder funds, index funds and ETFs, arbitrage funds, and
multi-asset funds are out of scope.

Practically: **bring your own debt / gold / international allocation** —
this report says nothing about them. Index funds are never recommended by
the ranking itself; the only place a passive product appears is the
"index wins this category" verdict, which tells you when the benchmark
beat the active funds and a cheap index fund/ETF is the more honest
choice. See the project README for how the pipeline works mechanically.\
"""

#: Metric columns the per-pick renderer displays. The glossary test pins
#: that every entry here has a GLOSSARY entry rendered into the report.
RENDERED_METRIC_COLUMNS = [
    "stage2_rank",
    "isin_growth",
    "composite_score",
    "n_considered",
    "ret_3y_median",
    "ret_3y_p25",
    "ret_5y_median",
    "ret_5y_p25",
    "alpha_3y_annualized",
    "alpha_confidence",
    "pick_minus_benchmark_3y",
    "sortino_3y",
    "info_ratio_3y",
    "capture_up",
    "capture_down",
    "capture_efficiency",
    "r_squared_3y",
    "beta_3y",
    "style_drift_3y",
    "active_share_median_1y",
    "ptr_latest",
    "aum_crore",
    "aum_impact_cost_days",
    "liquidity_flag",
    "max_dd_3y_pct",
    "max_dd_3y_recovery_days",
    "max_dd_5y_pct",
    "max_dd_5y_recovery_days",
    "missing_disclosures",
    "overlap_flag",
    "max_overlap_pct",
]

GLOSSARY: dict[str, str] = {
    "stage2_rank": "The fund's final position within its category after the "
    "portfolio-level re-rank. 1 is the category's top pick.",
    "isin_growth": "The exchange identifier of the Direct-Growth unit class "
    "— the exact security you transact when you buy this fund.",
    "composite_score": "Weighted blend of all the fund's standardized "
    "metrics. 0 means dead-average for the category; +1 means clearly "
    "better than peers. Only comparable within a category.",
    "n_considered": "How many funds in the category were considered in the "
    "final-stage pool the pick was ranked against.",
    "ret_3y_median": "Typical yearly return over 3-year stretches: the "
    "middle outcome of every rolling 3-year holding period in the fund's "
    "history — what a patient investor most often earned per year.",
    "ret_3y_p25": "The weaker stretches: a quarter of all rolling 3-year "
    "periods returned less than this per year. A floor-ish (not worst-case) "
    "outcome.",
    "ret_5y_median": "Same as ret_3y_median but over rolling 5-year holding "
    "periods — the long-haul view.",
    "ret_5y_p25": "A quarter of all rolling 5-year periods returned less "
    "than this per year.",
    "alpha_3y_annualized": "The fund's edge over its benchmark index after "
    "accounting for how much market risk it took — extra % per year the "
    "manager added beyond what the index explains.",
    "alpha_confidence": "What share of the measured 3-year windows showed a "
    "statistically clear edge (0–100%). High confidence means the edge "
    "showed up consistently, not in one lucky stretch.",
    "pick_minus_benchmark_3y": "The pick's typical 3-year yearly return "
    "minus the benchmark index's own typical 3-year return — the plain "
    "margin over just buying the index.",
    "sortino_3y": "Return earned per unit of *downside* wobble (upside "
    "swings don't count against it). Higher is better; above ~1 means "
    "returns comfortably beat the bad days.",
    "info_ratio_3y": "How steadily the fund beats its benchmark: average "
    "outperformance divided by how erratic that outperformance is. Above "
    "~0.5 is solid, above 1 excellent.",
    "capture_up": "Share of the benchmark's gains the fund captured in "
    "rising markets. 1.10 = it gained 10% more than the index when markets "
    "rose.",
    "capture_down": "Share of the benchmark's falls the fund suffered in "
    "falling markets. 0.90 = it fell only 90% as much as the index.",
    "capture_efficiency": "Up-capture divided by down-capture. Above 1 "
    "means the fund wins more of the rallies than it loses in the "
    "sell-offs — the asymmetry you actually want.",
    "r_squared_3y": "How much of the fund's movement its benchmark "
    "explains (0–1). Low values mean the benchmark is a loose yardstick "
    "and benchmark-relative numbers (alpha) deserve less trust.",
    "beta_3y": "How hard the fund moves when its benchmark moves. 1.0 = "
    "moves in line; 1.2 = amplifies market swings by 20%; 0.8 = damps "
    "them.",
    "style_drift_3y": "How much the portfolio's investment style wandered "
    "over 3 years. High drift means you may not be holding what the "
    "category label promises.",
    "active_share_median_1y": "Share of the portfolio that differs from "
    "the benchmark index (0–1). Low active share means paying active fees "
    "for index-like holdings.",
    "ptr_latest": "Portfolio turnover ratio: how much of the portfolio the "
    "manager replaced in a year. 1.0 means 100% — high turnover adds "
    "hidden trading costs.",
    "aum_crore": "Fund size: assets under management in ₹ crore. Very "
    "large funds can struggle to trade nimbly; very small ones may close "
    "or merge.",
    "aum_impact_cost_days": "Liquidity stress test: roughly how many "
    "trading days it would take, at recent traded volumes, to unwind the "
    "fund's least-liquid large positions without moving prices.",
    "liquidity_flag": "OK / HIGH / SEVERE tiering of the days-to-exit "
    "number above. HIGH = over ~30 trading days; SEVERE = over ~90.",
    "max_dd_3y_pct": "Worst peak-to-trough fall in the fund's value over "
    "the last 3 years. The number to stare at when judging whether you "
    "could hold through a bad year.",
    "max_dd_3y_recovery_days": "How many calendar days the fund took to "
    "climb back to its previous peak after that worst 3-year fall. Blank "
    "means it had not recovered by the end of the window.",
    "max_dd_5y_pct": "Worst peak-to-trough fall over the last 5 years "
    "(usually includes a full market cycle).",
    "max_dd_5y_recovery_days": "Calendar days to recover the 5-year worst "
    "fall; blank means not yet recovered.",
    "missing_disclosures": "Portfolio metrics this fund's AMC did not "
    "publish (or that could not be parsed). The fund was scored neutral on "
    "them — flagged, never silently rewarded.",
    "overlap_flag": "True when this fund's stock holdings overlap more "
    "than the threshold (default 30%) with another shortlisted fund. "
    "Owning both adds less diversification than it appears.",
    "max_overlap_pct": "The largest holdings overlap this fund has with "
    "any other shortlisted fund, in % of portfolio weight.",
    "z_* columns": "Standardized scores behind the composite: how many "
    "category standard deviations a fund sits above (+) or below (−) the "
    "category average on a metric. 0 = average for the category.",
}


# ---------------------------------------------------------------------------
# Value helpers — tolerant of missing columns / empty cells everywhere.
# ---------------------------------------------------------------------------

def _s(row: dict, col: str) -> str | None:
    v = row.get(col)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _f(row: dict, col: str) -> float | None:
    v = _s(row, col)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _i(row: dict, col: str) -> int | None:
    v = _f(row, col)
    return int(v) if v is not None else None


def _b(row: dict, col: str) -> bool:
    v = _s(row, col)
    return v is not None and v.lower() in ("true", "1")


def fmt_pct(v: float | None, *, signed: bool = False, digits: int = 1) -> str:
    """Format a FRACTION (0.215 → '21.5%')."""
    if v is None:
        return DASH
    sign = "+" if (signed and v >= 0) else ""
    return f"{sign}{v * 100:.{digits}f}%"


def fmt_points_pct(v: float | None, *, digits: int = 1) -> str:
    """Format a number that is ALREADY in percent units (20.0 → '20.0%')."""
    if v is None:
        return DASH
    return f"{v:.{digits}f}%"


def fmt_num(v: float | None, digits: int = 2) -> str:
    return DASH if v is None else f"{v:.{digits}f}"


def fmt_crore(v: float | None) -> str:
    return DASH if v is None else f"₹{v:,.0f} crore"


# ---------------------------------------------------------------------------
# Assembly — file-based, every artifact optional.
# ---------------------------------------------------------------------------

def _read_artifact(path: Path, *, comment_prefix: str | None = None) -> pl.DataFrame:
    """One artifact CSV with every column read as Utf8 (schema-drift-proof;
    numbers are parsed at render time). Empty frame when absent/header-only."""
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_csv(path, infer_schema_length=0, comment_prefix=comment_prefix)
    except pl.exceptions.NoDataError:
        return pl.DataFrame()


@dataclass
class RunData:
    """Everything ``render_report`` needs, assembled from one run dir."""

    run_dir: Path
    picks: pl.DataFrame = field(default_factory=pl.DataFrame)
    picks_note: str | None = None  # set when stage2 fallback was used
    coverage: pl.DataFrame = field(default_factory=pl.DataFrame)
    excluded: pl.DataFrame = field(default_factory=pl.DataFrame)
    breaches: pl.DataFrame = field(default_factory=pl.DataFrame)
    breaches_note: str | None = None  # set when derived from overlap_pairs
    passive: pl.DataFrame = field(default_factory=pl.DataFrame)


def load_run(run_dir: Path) -> RunData:
    """Assemble a run directory's artifacts. Never raises on absent files."""
    data = RunData(run_dir=run_dir)

    picks = _read_artifact(run_dir / "stage3" / "mf_report.csv")
    if picks.is_empty():
        picks = _read_artifact(run_dir / "stage2" / "mf_report.csv")
        if not picks.is_empty():
            data.picks_note = STAGE2_FALLBACK_NOTE
    data.picks = picks

    data.coverage = _read_artifact(run_dir / "stage2" / "coverage.csv")
    data.excluded = _read_artifact(run_dir / "stage1" / "excluded.csv")
    data.passive = _read_artifact(
        run_dir / "passive_alternative.csv", comment_prefix="#"
    )

    breaches = _read_artifact(run_dir / "stage3" / "overlap_breaches.csv")
    if breaches.is_empty():
        pairs = _read_artifact(run_dir / "stage3" / "overlap_pairs.csv")
        if (
            not pairs.is_empty()
            and not picks.is_empty()
            and {"scheme_code_a", "scheme_code_b", "overlap_pct"} <= set(pairs.columns)
            and "scheme_code" in picks.columns
        ):
            in_report = set(picks["scheme_code"].to_list())
            breaches = pairs.filter(
                pl.col("scheme_code_a").is_in(in_report)
                & pl.col("scheme_code_b").is_in(in_report)
                & pl.col("overlap_pct").is_not_null()
            )
            if not breaches.is_empty():
                data.breaches_note = (
                    "derived from stage3/overlap_pairs.csv (overlap_breaches.csv "
                    "absent), restricted to pairs where both funds are in the "
                    "final report"
                )
    data.breaches = breaches
    return data


def _sorted_picks(picks: pl.DataFrame) -> pl.DataFrame:
    """Sort by category then final rank, tolerating absent rank columns."""
    if picks.is_empty() or "canonical_category" not in picks.columns:
        return picks
    df = picks
    rank_col = next(
        (c for c in ("stage2_rank", "rank", "stage1_rank") if c in df.columns), None
    )
    if rank_col:
        df = df.with_columns(
            pl.col(rank_col).cast(pl.Float64, strict=False).alias("_rank_sort")
        ).sort(["canonical_category", "_rank_sort"], nulls_last=True).drop("_rank_sort")
    else:
        df = df.sort("canonical_category")
    return df


def _coverage_map(coverage: pl.DataFrame) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if coverage.is_empty() or "canonical_category" not in coverage.columns:
        return out
    for row in coverage.iter_rows(named=True):
        cat = _s(row, "canonical_category")
        if cat:
            out[cat] = row
    return out


def _passive_map(passive: pl.DataFrame) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if passive.is_empty() or "canonical_category" not in passive.columns:
        return out
    for row in passive.iter_rows(named=True):
        cat = _s(row, "canonical_category")
        if cat:
            out[cat] = row
    return out


def _missing_disclosure_labels(row: dict) -> list[str]:
    """Plain-language labels of this row's unpublished disclosures.

    Column path: the ``missing_disclosures`` ';'-joined column (D3).
    Fallback (older runs): any null among DISCLOSURE_FALLBACK_METRICS —
    only when the run carries those metric columns at all (identical
    semantics pre/post D3)."""
    if "missing_disclosures" in row:
        raw = _s(row, "missing_disclosures")
        metrics = [m for m in (raw or "").split(";") if m.strip()]
    else:
        present = [m for m in DISCLOSURE_FALLBACK_METRICS if m in row]
        metrics = [m for m in present if _f(row, m) is None]
    return [DISCLOSURE_METRIC_LABELS.get(m, m) for m in metrics]


# ---------------------------------------------------------------------------
# Rendering — per-pick metric block.
# ---------------------------------------------------------------------------

def _alpha_confidence_phrase(conf: float | None) -> str:
    if conf is None:
        return ""
    if conf >= 0.75:
        word = "high"
    elif conf >= 0.40:
        word = "moderate"
    else:
        word = "low"
    return (
        f" Confidence: {word} — the edge was statistically clear in "
        f"{conf * 100:.0f}% of the measured windows."
    )


def _render_returns_bullets(row: dict) -> list[str]:
    out = []
    m3, p3 = _f(row, "ret_3y_median"), _f(row, "ret_3y_p25")
    m5, p5 = _f(row, "ret_5y_median"), _f(row, "ret_5y_p25")
    out.append(
        f"- **Typical yearly return (3-year stretches): {fmt_pct(m3)}.** "
        f"Median across every rolling 3-year holding period; the weaker "
        f"quarter of those periods still returned {fmt_pct(p3)}/yr or less."
    )
    out.append(
        f"- **Typical yearly return (5-year stretches): {fmt_pct(m5)}** "
        f"(weaker quarter of periods: {fmt_pct(p5)}/yr or less)."
    )
    return out


def _render_alpha_bullet(row: dict) -> str:
    alpha = _f(row, "alpha_3y_annualized")
    bench = _s(row, "benchmark_ticker") or "its benchmark"
    line = (
        f"- **Edge over the benchmark (alpha): "
        f"{fmt_pct(alpha, signed=True)}/yr** vs {bench}, after adjusting "
        f"for market risk taken."
    )
    line += _alpha_confidence_phrase(_f(row, "alpha_confidence"))
    margin = _f(row, "pick_minus_benchmark_3y")
    if margin is not None:
        direction = "ahead of" if margin >= 0 else "behind"
        line += (
            f" In plain terms its typical 3-year return ran "
            f"{fmt_pct(abs(margin))}/yr {direction} the index itself."
        )
    return line


def _render_risk_bullets(row: dict) -> list[str]:
    out = []
    sortino = _f(row, "sortino_3y")
    out.append(
        f"- **Return per unit of downside risk (Sortino): {fmt_num(sortino)}.** "
        f"Above ~1 means returns comfortably outweighed the bad days; "
        f"higher is better."
    )
    ir = _f(row, "info_ratio_3y")
    out.append(
        f"- **Consistency of beating the benchmark (information ratio): "
        f"{fmt_num(ir)}.** Above ~0.5 is solid; above 1 is excellent."
    )
    cu, cd, ce = _f(row, "capture_up"), _f(row, "capture_down"), _f(row, "capture_efficiency")
    out.append(
        f"- **Up/down market behaviour:** captured {fmt_pct(cu, digits=0)} of "
        f"the benchmark's rises but only {fmt_pct(cd, digits=0)} of its falls "
        f"(efficiency {fmt_num(ce)}; above 1.00 means it wins rallies faster "
        f"than it loses sell-offs)."
    )
    r2, beta = _f(row, "r_squared_3y"), _f(row, "beta_3y")
    out.append(
        f"- **Benchmark fit:** the index explains {fmt_pct(r2, digits=0)} of "
        f"this fund's movement (R²), with a market sensitivity (beta) of "
        f"{fmt_num(beta)} — 1.00 moves in line with the index."
    )
    return out


def _render_drawdown_bullet(row: dict) -> str:
    dd3, rec3 = _f(row, "max_dd_3y_pct"), _i(row, "max_dd_3y_recovery_days")
    dd5, rec5 = _f(row, "max_dd_5y_pct"), _i(row, "max_dd_5y_recovery_days")

    def _one(dd: float | None, rec: int | None, label: str) -> str:
        if dd is None:
            return f"{label}: {DASH}"
        rec_txt = (
            f"recovered in ~{rec} days" if rec is not None
            else "had not recovered by the end of the window"
        )
        return f"{label}: fell {fmt_points_pct(dd)} peak-to-trough, {rec_txt}"
    return (
        f"- **Worst historical fall (drawdown):** {_one(dd3, rec3, 'last 3y')}; "
        f"{_one(dd5, rec5, 'last 5y')}. This is the behavioral test — could "
        f"you hold through that?"
    )


def _render_liquidity_bullet(row: dict) -> str:
    days = _f(row, "aum_impact_cost_days")
    flag = (_s(row, "liquidity_flag") or "").upper()
    if days is None and not flag:
        return (
            "- **Liquidity (days to exit):** — (no holdings/volume data "
            "for this fund)."
        )
    days_txt = f"~{days:.0f}" if days is not None else DASH
    line = (
        f"- **Liquidity (days to exit): {days_txt} trading days.** At recent "
        f"traded volumes, exiting the least-liquid large positions would "
        f"take {days_txt} trading days."
    )
    if flag:
        note = LIQUIDITY_FLAG_NOTES.get(flag, "")
        line += f" Flag: **{flag}**{' — ' + note if note else ''}."
    return line


def _render_stewardship_bullet(row: dict) -> str:
    ptr = _f(row, "ptr_latest")
    drift = _f(row, "style_drift_3y")
    ash = _f(row, "active_share_median_1y")
    ptr_txt = f"{ptr:.2f}x/yr" if ptr is not None else DASH
    ash_txt = fmt_pct(ash, digits=0) if ash is not None else DASH
    return (
        f"- **Portfolio stewardship:** turnover {ptr_txt} (how much the "
        f"manager trades), style drift {fmt_num(drift)} (lower = stays true "
        f"to its label), active share {ash_txt} (how different from the "
        f"index it really is)."
    )


def _overlap_partner_summary(row: dict, limit: int = 3) -> str:
    raw = _s(row, "overlap_breaches") or ""
    partners = [p.strip() for p in raw.split(";") if p.strip()]
    shown = "; ".join(partners[:limit])
    extra = len(partners) - limit
    if extra > 0:
        shown += f"; … and {extra} more"
    return shown


def _render_flag_bullets(row: dict) -> list[str]:
    out = []
    labels = _missing_disclosure_labels(row)
    if labels:
        out.append(
            f"- **PARTIAL DISCLOSURE:** the AMC did not publish "
            f"{', '.join(labels)}. The fund was scored neutral on the "
            f"missing data — treat its rank with mild extra caution."
        )
    if _b(row, "overlap_flag"):
        n = _i(row, "n_overlap_breaches")
        worst = _f(row, "max_overlap_pct")
        partners = _overlap_partner_summary(row)
        line = (
            f"- **OVERLAP WARNING:** holds largely the same stocks as "
            f"{n if n is not None else 'other'} other shortlisted "
            f"fund(s) (worst overlap {fmt_points_pct(worst)})."
        )
        if partners:
            line += f" Biggest overlaps: {partners}."
        line += " Owning both adds less diversification than it appears."
        out.append(line)
    dq = (_s(row, "data_quality_flag") or "").upper()
    if dq and dq != "GOOD":
        out.append(
            f"- **DATA QUALITY:** this fund's inputs carry a "
            f"'{dq}' flag — some numbers rest on thinner data."
        )
    return out


def _render_pick(row: dict, fallback_rank: int, n_considered: int | None) -> list[str]:
    name = _s(row, "scheme_name") or f"(scheme {_s(row, 'scheme_code') or '?'})"
    rank = _i(row, "stage2_rank") or _i(row, "rank") or fallback_rank
    n_cons = _i(row, "n_considered") or n_considered

    lines = [f"### {rank}. {name}", ""]
    cohort = f"rank {rank} of {n_cons} considered" if n_cons else f"rank {rank}"
    lines.append(
        f"**{cohort.capitalize()}** in {_s(row, 'canonical_category') or 'category'} | "
        f"Scheme code: {_s(row, 'scheme_code') or DASH} | "
        f"ISIN (Direct-Growth): **{_s(row, 'isin_growth') or DASH}** | "
        f"Composite score: {fmt_num(_f(row, 'composite_score'))}"
    )
    lines.append("")
    lines.extend(_render_returns_bullets(row))
    lines.append(_render_alpha_bullet(row))
    lines.extend(_render_risk_bullets(row))
    lines.append(_render_drawdown_bullet(row))
    lines.append(
        f"- **Fund size:** {fmt_crore(_f(row, 'aum_crore'))} under management."
    )
    lines.append(_render_liquidity_bullet(row))
    lines.append(_render_stewardship_bullet(row))
    lines.extend(_render_flag_bullets(row))
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Rendering — category sections.
# ---------------------------------------------------------------------------

def _index_wins_marker(prow: dict) -> str:
    bench = _s(prow, "benchmark_ticker") or "the benchmark index"
    excess = _f(prow, "median_fund_excess_3y")
    margin = (
        f" (median active fund trailed it by {fmt_pct(abs(excess))}/yr "
        f"over rolling 3-year periods)" if excess is not None else ""
    )
    return (
        f"> **THE INDEX WON THIS CATEGORY.** {bench} beat the typical "
        f"active fund here{margin}. **Consider a low-cost index fund or ETF "
        f"tracking {bench} instead of the active picks below** — they must "
        f"out-earn their fees just to match it."
    )


def _category_caveats(first_row: dict, prow: dict | None) -> list[str]:
    out = []
    if prow is not None and _b(prow, "index_wins"):
        out.extend([_index_wins_marker(prow), ""])
    if _b(first_row, "benchmark_fit_low_confidence"):
        out.extend([
            "> **Benchmark-fit caveat:** funds in this category track their "
            "assigned benchmark loosely, so the 'edge over the benchmark' "
            "(alpha) numbers below are low-confidence. Lean on the absolute "
            "return and drawdown numbers instead.",
            "",
        ])
    synthetic = _b(first_row, "benchmark_is_synthetic") or (
        prow is not None and _b(prow, "benchmark_is_synthetic")
    )
    if synthetic:
        out.extend([
            "> **Synthetic benchmark:** no investable total-return index "
            "exists for this category, so it is measured against a "
            "constructed blend (equity index + T-bills). Benchmark-beating "
            "numbers here are likely overstated.",
            "",
        ])
    return out


def _render_category_section(
    cat: str,
    rows: list[dict],
    cov: dict | None,
    prow: dict | None,
) -> list[str]:
    lines = [f"## {cat}", ""]

    n_considered = _i(cov, "n_considered") if cov else None
    if n_considered is None and rows:
        n_considered = _i(rows[0], "n_considered")
    n_survived = (_i(cov, "n_survived") if cov else None) or len(rows)
    if n_considered:
        lines.extend([
            f"{len(rows)} pick(s) shown, from {n_considered} fund(s) "
            f"considered in the final pool ({n_survived} survived the "
            f"quality gates).",
            "",
        ])

    lines.extend(_category_caveats(rows[0] if rows else {}, prow))

    for idx, row in enumerate(rows, start=1):
        lines.extend(_render_pick(row, fallback_rank=idx, n_considered=n_considered))
    return lines


# ---------------------------------------------------------------------------
# Rendering — exception sections (E5). Each degrades to a one-line note.
# ---------------------------------------------------------------------------

def _absent(artifact: str) -> list[str]:
    return [f"_{artifact} not present in this run — nothing to show._", ""]


def _exclusion_note(reason: str) -> str:
    if reason.startswith("FILTER:"):
        return f"failed the '{reason.split(':', 1)[1]}' quality floor"
    if reason.startswith("MISSING_CORE_METRIC"):
        missing = reason.split(":", 1)[1] if ":" in reason else ""
        return (
            f"a core metric ({missing}) could not be computed"
            if missing else "a core metric could not be computed"
        )
    return EXCLUSION_REASON_NOTES.get(reason, reason)


def _render_excluded_section(excluded: pl.DataFrame) -> list[str]:
    lines = ["## Excluded — insufficient data", ""]
    if excluded.is_empty() or "exclusion_reason" not in excluded.columns:
        return lines + _absent("stage1/excluded.csv artifact")
    lines.extend([
        "These funds were not ranked because the pipeline could not measure "
        "them cleanly. Excluded is not the same as bad — most are simply "
        "too young or stopped publishing usable data.",
        "",
    ])
    # Summary by reason.
    reasons: dict[str, int] = {}
    for row in excluded.iter_rows(named=True):
        r = _s(row, "exclusion_reason") or "(unknown)"
        reasons[r] = reasons.get(r, 0) + 1
    lines.append("| Reason | Funds | What it means |")
    lines.append("|---|---|---|")
    for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {r} | {n} | {_exclusion_note(r)} |")
    lines.append("")
    # Per category.
    by_cat: dict[str, list[str]] = {}
    for row in excluded.iter_rows(named=True):
        cat = _s(row, "canonical_category") or "(uncategorized)"
        name = _s(row, "scheme_name") or _s(row, "scheme_code") or "?"
        reason = _s(row, "exclusion_reason") or "?"
        by_cat.setdefault(cat, []).append(f"{name} [{reason}]")
    for cat in sorted(by_cat):
        entries = by_cat[cat]
        lines.append(f"- **{cat}** ({len(entries)}): " + "; ".join(entries))
    lines.append("")
    return lines


def _render_partial_disclosure_section(picks: pl.DataFrame) -> list[str]:
    lines = ["## Partial disclosure data", ""]
    if picks.is_empty():
        return lines + _absent("ranked report artifact")
    flagged = []
    for row in picks.iter_rows(named=True):
        labels = _missing_disclosure_labels(row)
        if labels:
            name = _s(row, "scheme_name") or _s(row, "scheme_code") or "?"
            cat = _s(row, "canonical_category") or "?"
            flagged.append(f"- **{name}** ({cat}): did not publish {', '.join(labels)}.")
    if not flagged:
        return lines + [
            "_Every fund in the final report published its full portfolio "
            "disclosures. Nothing to flag._",
            "",
        ]
    lines.extend([
        "These picks are missing one or more AMC portfolio disclosures. They "
        "were scored neutral (never rewarded, never silently dropped) on the "
        "missing metric — their ranks carry mild extra uncertainty.",
        "",
    ])
    lines.extend(flagged)
    lines.append("")
    return lines


def _render_overlap_section(
    breaches: pl.DataFrame, note: str | None, threshold_note: str = "30%",
) -> list[str]:
    lines = ["## Portfolio overlap warnings", ""]
    if breaches.is_empty():
        return lines + _absent(
            "stage3/overlap_breaches.csv (and no derivable overlap_pairs.csv)"
        )
    if note:
        lines.extend([f"_Note: {note}._", ""])
    lines.extend([
        f"Pairs of shortlisted funds whose stock portfolios overlap more "
        f"than {threshold_note} by weight. No fund is dropped for overlap — "
        f"but owning both sides of a pair below buys you less "
        f"diversification than two fund names suggest.",
        "",
    ])
    total = breaches.height
    for row in breaches.head(MAX_OVERLAP_PAIRS_RENDERED).iter_rows(named=True):
        name_a = _s(row, "scheme_name_a") or _s(row, "scheme_code_a") or "?"
        name_b = _s(row, "scheme_name_b") or _s(row, "scheme_code_b") or "?"
        cat_a = _s(row, "category_a") or "?"
        cat_b = _s(row, "category_b") or "?"
        pct = fmt_points_pct(_f(row, "overlap_pct"))
        shared = _s(row, "top_shared_holdings")
        line = (
            f"- **{name_a}** ({cat_a}) + **{name_b}** ({cat_b}): "
            f"**{pct} overlap**"
        )
        if shared:
            line += f" — top shared holdings: {shared}"
        lines.append(line)
    if total > MAX_OVERLAP_PAIRS_RENDERED:
        lines.append(
            f"- … and {total - MAX_OVERLAP_PAIRS_RENDERED} more pair(s) — "
            f"full list in `stage3/overlap_breaches.csv`."
        )
    lines.append("")
    return lines


def _render_index_wins_section(passive: pl.DataFrame) -> list[str]:
    lines = ["## Where the index fund wins", ""]
    if passive.is_empty() or "index_wins" not in passive.columns:
        return lines + _absent("passive_alternative.csv artifact")
    winners = [
        row for row in passive.iter_rows(named=True) if _b(row, "index_wins")
    ]
    if not winners:
        return lines + [
            "_In every covered category the median active fund beat its "
            "benchmark index over rolling 3-year periods. No passive "
            "verdicts this run._",
            "",
        ]
    lines.extend([
        "In these categories the benchmark index itself returned more than "
        "the median active fund over rolling 3-year periods. **Consider a "
        "low-cost index fund or ETF instead of an active pick here** — the "
        "average manager did not earn their fee.",
        "",
        "| Category | Benchmark | Index 3y/yr | Median fund 3y/yr | Fund shortfall |",
        "|---|---|---|---|---|",
    ])
    for row in winners:
        lines.append(
            "| {cat} | {bench} | {idx} | {fund} | {short}/yr |".format(
                cat=_s(row, "canonical_category") or "?",
                bench=_s(row, "benchmark_ticker") or "?",
                idx=fmt_pct(_f(row, "benchmark_ret_3y_median")),
                fund=fmt_pct(_f(row, "fund_ret_3y_median")),
                short=fmt_pct(_f(row, "median_fund_excess_3y"), signed=True),
            )
        )
    lines.append("")
    notes = {
        _s(row, "note") for row in winners if _s(row, "note")
    }
    for n in sorted(notes):
        lines.append(f"_Note: {n}._")
    if notes:
        lines.append("")
    return lines


def _render_young_funds_section(young: pl.DataFrame | None) -> list[str]:
    lines = ["## Young funds — not yet eligible", ""]
    if young is None:
        return lines + [
            "_Young-fund eligibility data was not queried for this report "
            "(offline run)._",
            "",
        ]
    if young.is_empty():
        return lines + [
            "_No rankable-category fund is younger than the eligibility "
            "window right now._",
            "",
        ]
    lines.extend([
        f"{young.height} fund(s) in rankable categories are less than 3 "
        f"years old — too young for their track record to mean anything, so "
        f"they are **not yet eligible**, not rejected. They enter the "
        f"ranking automatically once they age in.",
        "",
    ])
    by_cat: dict[str, list[str]] = {}
    for row in young.iter_rows(named=True):
        cat = str(row.get("canonical_category") or "(uncategorized)")
        name = str(row.get("scheme_name") or row.get("scheme_code") or "?")
        incep = row.get("inception_date")
        isin = row.get("isin_growth") or DASH
        incep_txt = str(incep) if incep else DASH
        by_cat.setdefault(cat, []).append(f"{name} (since {incep_txt}, ISIN {isin})")
    for cat in sorted(by_cat):
        entries = by_cat[cat]
        lines.append(f"- **{cat}** ({len(entries)}): " + "; ".join(entries))
    lines.append("")
    return lines


def _render_glossary_section() -> list[str]:
    lines = [
        "## Glossary",
        "",
        "Plain-language meaning of every metric this report shows.",
        "",
    ]
    for col, meaning in GLOSSARY.items():
        lines.append(f"- `{col}` — {meaning}")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Top-level renderer + builder.
# ---------------------------------------------------------------------------

def render_report(
    data: RunData,
    as_of: date,
    young_funds: pl.DataFrame | None = None,
) -> str:
    """Render the full REPORT.md text from assembled run data."""
    lines: list[str] = [
        f"# Mutual fund shortlist — investor report ({as_of.isoformat()})",
        "",
        DISCLAIMER_BLOCK,
        "",
        HOW_TO_READ_SECTION,
        "",
    ]
    if data.picks_note:
        lines.extend([f"> **NOTE:** {data.picks_note}", ""])

    picks = _sorted_picks(data.picks)
    cov_map = _coverage_map(data.coverage)
    pas_map = _passive_map(data.passive)

    if picks.is_empty() or "canonical_category" not in picks.columns:
        lines.extend([
            "## Picks",
            "",
            "_No ranked picks were found in this run directory "
            "(no stage3/ or stage2/ mf_report.csv)._",
            "",
        ])
    else:
        by_cat: dict[str, list[dict]] = {}
        for row in picks.iter_rows(named=True):
            cat = _s(row, "canonical_category") or "(uncategorized)"
            by_cat.setdefault(cat, []).append(row)
        for cat in sorted(by_cat):
            lines.extend(
                _render_category_section(
                    cat, by_cat[cat], cov_map.get(cat), pas_map.get(cat)
                )
            )

    lines.extend(_render_excluded_section(data.excluded))
    lines.extend(_render_partial_disclosure_section(data.picks))
    lines.extend(_render_overlap_section(data.breaches, data.breaches_note))
    lines.extend(_render_index_wins_section(data.passive))
    lines.extend(_render_young_funds_section(young_funds))
    lines.extend([TAXES_SECTION, ""])
    lines.extend([SWITCHING_SECTION, ""])
    lines.extend([CADENCE_SECTION, ""])
    lines.extend([SCOPE_SECTION, ""])
    lines.extend(_render_glossary_section())
    return "\n".join(lines)


def build_report(
    run_dir: Path,
    as_of: date,
    young_funds: pl.DataFrame | None = None,
) -> Path:
    """Assemble + render one run directory into ``<run_dir>/REPORT.md``."""
    data = load_run(run_dir)
    text = render_report(data, as_of=as_of, young_funds=young_funds)
    out = run_dir / REPORT_FILENAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    log.info(
        "rank.report_written",
        path=str(out),
        n_picks=data.picks.height,
        stage2_fallback=bool(data.picks_note),
    )
    return out
