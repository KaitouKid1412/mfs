"""Stage 3 — iterative portfolio-overlap drop.

Stage 3 takes the Stage 2 survivors (≤5 per category × ~26 categories),
computes pairwise portfolio overlap across every pair (cross-category
included), and **iteratively drops** the lower-Stage-2-ranked fund of
the highest-overlap pair until no remaining pair exceeds the threshold.

Outputs:
  1. ``stage3/<category>.csv`` (+ ``.parquet``) — Stage 2 survivors that
     weren't dropped, per category. Ordered by Stage 2 rank.
  2. ``stage3/dropped.csv`` — funds dropped due to overlap, with the
     identity of the fund kept and the top shared holdings.
  3. ``stage3/overlap_pairs.csv`` — every pair > threshold encountered
     during iteration (informational).
  4. ``stage3/mf_report.csv`` (+ ``.parquet``) — consolidated final list.
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


def _stage2_rank(row: dict) -> int:
    """Higher rank-number = worse (rank 1 is the top pick). Treat a missing
    stage2_rank as the worst rank so the calling code drops it first when
    paired with an explicitly ranked fund."""
    r = row.get("stage2_rank")
    return int(r) if r is not None else 10**9


def apply_stage3(
    survivors: pl.DataFrame,
    holdings_loader: Callable[[str], pl.DataFrame],
    threshold_pct: float = DEFAULT_OVERLAP_THRESHOLD,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Run iterative overlap-drop on the Stage 2 survivor set.

    Returns (final_picks, drops, overlap_pairs_log):
        - ``final_picks``: surviving rows, same schema as input.
        - ``drops``: one row per fund dropped, with the identity of the
          kept counterpart and the top shared holdings.
        - ``overlap_pairs_log``: every pair > threshold seen during
          iteration (informational; includes pairs whose lower-ranked side
          was already dropped earlier).
    """
    if survivors.is_empty() or survivors.height < 2:
        return survivors, pl.DataFrame(), pl.DataFrame()

    cache: dict[str, pl.DataFrame] = {}

    def _h(scheme_code: str) -> pl.DataFrame:
        if scheme_code not in cache:
            cache[scheme_code] = holdings_loader(scheme_code)
        return cache[scheme_code]

    # Materialize the row dicts so we can iteratively drop entries.
    rows_by_code: dict[str, dict] = {
        r["scheme_code"]: r for r in survivors.iter_rows(named=True)
    }

    # Pre-compute every pair's overlap once. We don't recompute after a
    # drop because dropping a fund only removes pairs that involve it —
    # the others are unchanged.
    pair_records: list[dict] = []
    pairs_by_codes: dict[tuple[str, str], dict] = {}
    for a_code, b_code in combinations(rows_by_code.keys(), 2):
        a = rows_by_code[a_code]
        b = rows_by_code[b_code]
        result: OverlapResult = pairwise_overlap(_h(a_code), _h(b_code))
        rec = {
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
        }
        pair_records.append(rec)
        pairs_by_codes[(a_code, b_code)] = rec

    # Iterative drop: pick the highest-overlap remaining pair > threshold,
    # drop the worse-stage2-rank fund of the pair.
    drops_log: list[dict] = []
    while True:
        candidate = None
        candidate_overlap = threshold_pct  # strict > threshold required
        for (a_code, b_code), rec in pairs_by_codes.items():
            if a_code not in rows_by_code or b_code not in rows_by_code:
                continue
            ov = rec.get("overlap_pct")
            if ov is None:
                continue
            if ov > candidate_overlap:
                candidate = (a_code, b_code, rec)
                candidate_overlap = ov
        if candidate is None:
            break
        a_code, b_code, rec = candidate
        a_row = rows_by_code[a_code]
        b_row = rows_by_code[b_code]
        # Drop the fund with the worse (higher-number) Stage 2 rank. Ties
        # broken by scheme_code asc (deterministic).
        a_rank = _stage2_rank(a_row)
        b_rank = _stage2_rank(b_row)
        if a_rank < b_rank:
            kept, dropped = a_row, b_row
        elif b_rank < a_rank:
            kept, dropped = b_row, a_row
        else:
            if a_code <= b_code:
                kept, dropped = a_row, b_row
            else:
                kept, dropped = b_row, a_row
        drops_log.append({
            "scheme_code_dropped": dropped["scheme_code"],
            "scheme_name_dropped": dropped.get("scheme_name"),
            "category_dropped": dropped["canonical_category"],
            "stage2_rank_dropped": dropped.get("stage2_rank"),
            "scheme_code_kept": kept["scheme_code"],
            "scheme_name_kept": kept.get("scheme_name"),
            "category_kept": kept["canonical_category"],
            "stage2_rank_kept": kept.get("stage2_rank"),
            "overlap_pct": rec.get("overlap_pct"),
            "top_shared_holdings": rec.get("top_shared_holdings"),
        })
        rows_by_code.pop(dropped["scheme_code"], None)

    remaining_codes = set(rows_by_code.keys())
    final_picks = survivors.filter(pl.col("scheme_code").is_in(list(remaining_codes)))
    drops_df = pl.DataFrame(drops_log) if drops_log else pl.DataFrame()
    pairs_log_df = (
        pl.DataFrame(pair_records).filter(
            (pl.col("overlap_pct") > threshold_pct)
            | pl.col("reason").is_not_null()
        ).sort("overlap_pct", descending=True, nulls_last=True)
        if pair_records else pl.DataFrame()
    )
    return final_picks, drops_df, pairs_log_df


def run(
    survivors: pl.DataFrame,
    holdings_loader: Callable[[str], pl.DataFrame],
    output_dir: Path,
    threshold_pct: float = DEFAULT_OVERLAP_THRESHOLD,
) -> dict:
    """End-to-end Stage 3: iterative drop + write artifacts."""
    final_picks, drops, overlap_pairs = apply_stage3(
        survivors, holdings_loader, threshold_pct=threshold_pct,
    )
    stage3_dir = output_dir / "stage3"
    stage3_dir.mkdir(parents=True, exist_ok=True)

    cat_paths: dict[str, Path] = {}
    report_chunks: list[pl.DataFrame] = []
    if not final_picks.is_empty() and "canonical_category" in final_picks.columns:
        sort_col = "stage2_rank" if "stage2_rank" in final_picks.columns else "scheme_code"
        for cat_tuple, grp in final_picks.group_by("canonical_category"):
            cat = cat_tuple[0]
            safe = "".join(c if c.isalnum() else "_" for c in cat)
            sorted_grp = grp.sort(sort_col)
            csv_path = stage3_dir / f"{safe}.csv"
            parquet_path = stage3_dir / f"{safe}.parquet"
            sorted_grp.write_csv(csv_path)
            sorted_grp.write_parquet(parquet_path, compression="zstd")
            cat_paths[cat] = csv_path
            report_chunks.append(sorted_grp)

    dropped_path = stage3_dir / "dropped.csv"
    if not drops.is_empty():
        drops.write_csv(dropped_path)
    else:
        pl.DataFrame({
            "scheme_code_dropped": pl.Series([], dtype=pl.Utf8),
            "scheme_code_kept": pl.Series([], dtype=pl.Utf8),
            "overlap_pct": pl.Series([], dtype=pl.Float64),
        }).write_csv(dropped_path)

    pairs_path = stage3_dir / "overlap_pairs.csv"
    if not overlap_pairs.is_empty():
        overlap_pairs.write_csv(pairs_path)
    else:
        pl.DataFrame({
            "scheme_code_a": pl.Series([], dtype=pl.Utf8),
            "scheme_code_b": pl.Series([], dtype=pl.Utf8),
            "overlap_pct": pl.Series([], dtype=pl.Float64),
        }).write_csv(pairs_path)

    if report_chunks:
        report = pl.concat(report_chunks, how="diagonal_relaxed").sort(
            ["canonical_category"] + (
                ["stage2_rank"]
                if all("stage2_rank" in c.columns for c in report_chunks) else []
            )
        )
        report.write_csv(stage3_dir / "mf_report.csv")
        report.write_parquet(stage3_dir / "mf_report.parquet", compression="zstd")

    n_dropped = int(drops.height) if not drops.is_empty() else 0
    log.info(
        "rank.stage3.done",
        n_initial=int(survivors.height),
        n_final=int(final_picks.height),
        n_dropped=n_dropped,
        threshold_pct=threshold_pct,
    )
    return {
        "stage3_dir": str(stage3_dir),
        "category_files": {k: str(v) for k, v in cat_paths.items()},
        "dropped_file": str(dropped_path),
        "overlap_pairs_file": str(pairs_path),
        "n_initial": int(survivors.height),
        "n_final": int(final_picks.height),
        "n_dropped": n_dropped,
        "final_picks": final_picks,
        "dropped": drops,
        "overlap_pairs": overlap_pairs,
    }
