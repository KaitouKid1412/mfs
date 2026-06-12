"""Stage 3 — portfolio-overlap flagging: keep both, flag the breach.

Locked decision D4 (2026-06-11, restoring the 2026-05-24 decision in
docs/phase3/two_stage_pipeline_plan.md: "keep both funds when overlap > 30%,
flag the breach (no auto-drop)"): Stage 3 takes the Stage 2 survivors
(≤5 per category × ~26 categories), computes pairwise portfolio overlap
across every pair (cross-category included), and KEEPS every fund. NO fund
is ever dropped for overlap — the old iterative cross-category drop emptied
5 of 26 categories (ELSS, Flexi Cap, Balanced Advantage, Equity Savings,
Contra) because their mandates structurally overlap Large/Flexi portfolios.

Pairs whose overlap strictly exceeds the threshold are *breaches*: both
funds stay in the final picks and are annotated with
``overlap_flag`` (any breach), ``max_overlap_pct`` (across all computed
pairs), ``n_overlap_breaches`` (breaching pairs the fund is in) and
``overlap_breaches`` (';'-joined ``<counterpart code> <name> (<category>)
@ <pct>%``).

Outputs:
  1. ``stage3/<category>.csv`` (+ ``.parquet``) — every Stage 2 survivor,
     per category, with the flag columns. Ordered by Stage 2 rank.
  2. ``stage3/overlap_breaches.csv`` — one row per breaching PAIR (both
     identities, Stage 2 ranks, overlap %, top shared holdings).
  3. ``stage3/overlap_pairs.csv`` — every pair > threshold plus pairs that
     could not be computed (``reason='missing_holdings'``). Informational.
  4. ``stage3/overlap_matrix.csv`` — ALL computed pairs, unfiltered
     (subset-buyers need the sub-threshold overlaps too).
  5. ``stage3/mf_report.csv`` (+ ``.parquet``) — consolidated final list.
"""

from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Callable

import polars as pl

from mfs.rank.overlap import OverlapResult, format_top_shared, pairwise_overlap
from mfs.utils.logging import get_logger

log = get_logger(__name__)

DEFAULT_OVERLAP_THRESHOLD = 30.0

# Flag columns appended to every final-pick row (defaults for funds with no
# breaching / computable pair).
_FLAG_DEFAULTS: dict[str, pl.Expr] = {
    "overlap_flag": pl.lit(False),
    "max_overlap_pct": pl.lit(None, dtype=pl.Float64),
    "n_overlap_breaches": pl.lit(0, dtype=pl.Int64),
    "overlap_breaches": pl.lit("", dtype=pl.Utf8),
}


def _compute_pair_records(
    survivors: pl.DataFrame,
    holdings_loader: Callable[[str], pl.DataFrame],
) -> list[dict]:
    """All-pairs overlap over the survivor set (each fund's holdings loaded
    once). One record per unordered pair, computed or not."""
    cache: dict[str, pl.DataFrame] = {}

    def _h(scheme_code: str) -> pl.DataFrame:
        if scheme_code not in cache:
            cache[scheme_code] = holdings_loader(scheme_code)
        return cache[scheme_code]

    rows_by_code: dict[str, dict] = {
        r["scheme_code"]: r for r in survivors.iter_rows(named=True)
    }
    pair_records: list[dict] = []
    for a_code, b_code in combinations(rows_by_code.keys(), 2):
        a = rows_by_code[a_code]
        b = rows_by_code[b_code]
        result: OverlapResult = pairwise_overlap(_h(a_code), _h(b_code))
        pair_records.append({
            "scheme_code_a": a_code,
            "scheme_name_a": a.get("scheme_name"),
            "category_a": a["canonical_category"],
            "stage2_rank_a": a.get("stage2_rank"),
            "scheme_code_b": b_code,
            "scheme_name_b": b.get("scheme_name"),
            "category_b": b["canonical_category"],
            "stage2_rank_b": b.get("stage2_rank"),
            "overlap_pct": result.overlap_pct,
            "n_overlapping_securities": result.n_overlapping_securities,
            "top_shared_holdings": format_top_shared(result.top_shared_holdings),
            "reason": result.reason,
        })
    return pair_records


def _breach_label(rec: dict, side: str) -> str:
    """Render one breach as seen from the *other* fund: side is the
    counterpart ('a' or 'b')."""
    return (
        f"{rec[f'scheme_code_{side}']} {rec[f'scheme_name_{side}']} "
        f"({rec[f'category_{side}']}) @ {rec['overlap_pct']}%"
    )


def _apply_stage3_full(
    survivors: pl.DataFrame,
    holdings_loader: Callable[[str], pl.DataFrame],
    threshold_pct: float = DEFAULT_OVERLAP_THRESHOLD,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """``apply_stage3`` plus the unfiltered all-pairs matrix frame."""
    if survivors.is_empty():
        return survivors, pl.DataFrame(), pl.DataFrame(), pl.DataFrame()
    if survivors.height < 2:
        final = survivors.with_columns(
            expr.alias(name) for name, expr in _FLAG_DEFAULTS.items()
        )
        return final, pl.DataFrame(), pl.DataFrame(), pl.DataFrame()

    pair_records = _compute_pair_records(survivors, holdings_loader)

    # Per-fund flag accumulation. A breach is overlap strictly > threshold.
    max_overlap: dict[str, float] = {}
    breach_labels: dict[str, list[tuple[float, str]]] = {}
    breach_records: list[dict] = []
    for rec in pair_records:
        ov = rec["overlap_pct"]
        if ov is None:
            continue
        for code in (rec["scheme_code_a"], rec["scheme_code_b"]):
            if code not in max_overlap or ov > max_overlap[code]:
                max_overlap[code] = ov
        if ov > threshold_pct:
            breach_records.append({
                k: rec[k] for k in (
                    "scheme_code_a", "scheme_name_a", "category_a",
                    "stage2_rank_a",
                    "scheme_code_b", "scheme_name_b", "category_b",
                    "stage2_rank_b",
                    "overlap_pct", "n_overlapping_securities",
                    "top_shared_holdings",
                )
            })
            breach_labels.setdefault(rec["scheme_code_a"], []).append(
                (ov, _breach_label(rec, "b"))
            )
            breach_labels.setdefault(rec["scheme_code_b"], []).append(
                (ov, _breach_label(rec, "a"))
            )

    def _joined_breaches(code: str) -> str:
        labels = sorted(breach_labels.get(code, []), key=lambda t: (-t[0], t[1]))
        return ";".join(label for _, label in labels)

    codes = survivors["scheme_code"].to_list()
    final = survivors.with_columns(
        pl.Series(
            "overlap_flag", [c in breach_labels for c in codes], dtype=pl.Boolean,
        ),
        pl.Series(
            "max_overlap_pct", [max_overlap.get(c) for c in codes],
            dtype=pl.Float64,
        ),
        pl.Series(
            "n_overlap_breaches",
            [len(breach_labels.get(c, [])) for c in codes], dtype=pl.Int64,
        ),
        pl.Series(
            "overlap_breaches", [_joined_breaches(c) for c in codes],
            dtype=pl.Utf8,
        ),
    )

    breaches_df = (
        pl.DataFrame(breach_records).sort(
            ["overlap_pct", "scheme_code_a", "scheme_code_b"],
            descending=[True, False, False],
        )
        if breach_records else pl.DataFrame()
    )
    pairs_log_df = (
        pl.DataFrame(pair_records).filter(
            (pl.col("overlap_pct") > threshold_pct)
            | pl.col("reason").is_not_null()
        ).sort(
            ["overlap_pct", "scheme_code_a", "scheme_code_b"],
            descending=[True, False, False], nulls_last=True,
        )
        if pair_records else pl.DataFrame()
    )
    matrix_df = (
        pl.DataFrame(pair_records).sort(
            ["overlap_pct", "scheme_code_a", "scheme_code_b"],
            descending=[True, False, False], nulls_last=True,
        )
        if pair_records else pl.DataFrame()
    )
    return final, breaches_df, pairs_log_df, matrix_df


def apply_stage3(
    survivors: pl.DataFrame,
    holdings_loader: Callable[[str], pl.DataFrame],
    threshold_pct: float = DEFAULT_OVERLAP_THRESHOLD,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Keep-both + flag (locked D4) on the Stage 2 survivor set.

    Returns (final_picks, breaches, overlap_pairs_log):
        - ``final_picks``: EVERY survivor row (same schema as input plus the
          ``overlap_flag`` / ``max_overlap_pct`` / ``n_overlap_breaches`` /
          ``overlap_breaches`` columns). Nothing is dropped.
        - ``breaches``: one row per breaching PAIR (> threshold) with both
          identities, Stage 2 ranks, overlap % and top shared holdings.
        - ``overlap_pairs_log``: every pair > threshold plus uncomputable
          pairs (``reason='missing_holdings'``). Informational.
    """
    final, breaches, pairs_log, _ = _apply_stage3_full(
        survivors, holdings_loader, threshold_pct=threshold_pct,
    )
    return final, breaches, pairs_log


def _write_pairs_csv(df: pl.DataFrame, path: Path) -> None:
    """Write a pair-shaped frame, or the header-only schema when empty."""
    if not df.is_empty():
        df.write_csv(path)
    else:
        pl.DataFrame({
            "scheme_code_a": pl.Series([], dtype=pl.Utf8),
            "scheme_code_b": pl.Series([], dtype=pl.Utf8),
            "overlap_pct": pl.Series([], dtype=pl.Float64),
        }).write_csv(path)


def run(
    survivors: pl.DataFrame,
    holdings_loader: Callable[[str], pl.DataFrame],
    output_dir: Path,
    threshold_pct: float = DEFAULT_OVERLAP_THRESHOLD,
) -> dict:
    """End-to-end Stage 3: keep-both + flag (D4) + write artifacts."""
    final_picks, breaches, overlap_pairs, matrix = _apply_stage3_full(
        survivors, holdings_loader, threshold_pct=threshold_pct,
    )
    stage3_dir = output_dir / "stage3"
    stage3_dir.mkdir(parents=True, exist_ok=True)

    cat_paths: dict[str, Path] = {}
    report_chunks: list[pl.DataFrame] = []
    if not final_picks.is_empty() and "canonical_category" in final_picks.columns:
        # A2-11 determinism: stage2_rank is the composite-desc order from
        # Stage 2; scheme_code asc is the universal tie-break backstop.
        sort_cols = (
            ["stage2_rank", "scheme_code"]
            if "stage2_rank" in final_picks.columns else ["scheme_code"]
        )
        for cat_tuple, grp in final_picks.group_by("canonical_category"):
            cat = cat_tuple[0]
            safe = "".join(c if c.isalnum() else "_" for c in cat)
            sorted_grp = grp.sort(sort_cols, nulls_last=True)
            csv_path = stage3_dir / f"{safe}.csv"
            parquet_path = stage3_dir / f"{safe}.parquet"
            sorted_grp.write_csv(csv_path)
            sorted_grp.write_parquet(parquet_path, compression="zstd")
            cat_paths[cat] = csv_path
            report_chunks.append(sorted_grp)

    breaches_path = stage3_dir / "overlap_breaches.csv"
    _write_pairs_csv(breaches, breaches_path)

    pairs_path = stage3_dir / "overlap_pairs.csv"
    _write_pairs_csv(overlap_pairs, pairs_path)

    matrix_path = stage3_dir / "overlap_matrix.csv"
    _write_pairs_csv(matrix, matrix_path)

    if report_chunks:
        sort_cols = ["canonical_category"] + (
            ["stage2_rank"]
            if all("stage2_rank" in c.columns for c in report_chunks) else []
        ) + ["scheme_code"]
        report = pl.concat(report_chunks, how="diagonal_relaxed").sort(
            sort_cols, nulls_last=True,
        )
        report.write_csv(stage3_dir / "mf_report.csv")
        report.write_parquet(stage3_dir / "mf_report.parquet", compression="zstd")

    n_flagged = (
        int(final_picks.filter(pl.col("overlap_flag")).height)
        if "overlap_flag" in final_picks.columns else 0
    )
    n_breach_pairs = int(breaches.height) if not breaches.is_empty() else 0
    log.info(
        "rank.stage3.done",
        n_initial=int(survivors.height),
        n_final=int(final_picks.height),
        n_flagged=n_flagged,
        n_breach_pairs=n_breach_pairs,
        threshold_pct=threshold_pct,
    )
    return {
        "stage3_dir": str(stage3_dir),
        "category_files": {k: str(v) for k, v in cat_paths.items()},
        "breaches_file": str(breaches_path),
        "overlap_pairs_file": str(pairs_path),
        "overlap_matrix_file": str(matrix_path),
        "n_initial": int(survivors.height),
        "n_final": int(final_picks.height),
        "n_flagged": n_flagged,
        "n_breach_pairs": n_breach_pairs,
        "final_picks": final_picks,
        "breaches": breaches,
        "overlap_pairs": overlap_pairs,
        "overlap_matrix": matrix,
    }
