# Active-share activation — status note (C4, 2026-06-13)

**Status: OPEN — time-gated, cannot be completed yet.** The 0.10 stage-2
active-share weight is still dormant universe-wide and activates only once a
third monthly `index_constituents_monthly` snapshot lands (the median needs
`MIN_SNAPSHOTS_FOR_MEDIAN = 3` matched months of *both* fund holdings and
benchmark constituents). On the current calendar that is **~2026-07-10**, after
the June holdings → June constituents derivation completes. Today is 2026-06-13.

## Current state (verified 2026-06-13)

| signal | value |
|--------|-------|
| distinct `index_constituents_monthly` months | **2** (need 3) |
| `computed_metrics` rows at as_of 2026-06-13 | 680 |
| rows with non-null `active_share_median_1y` | **0** (dormant, as expected) |

So `active_share` is renormalize-only everywhere (it does not yet draw the
disclosure penalty), exactly the pre-activation behavior Stage 2 documents
(`rank/stage2.py` DISCLOSURE_METRICS note). The `rank_deep` banner prints
`n_matched_months=X/3` until the third month lands.

## Why this can't be closed today

`active_share` needs 3 monthly snapshots; only 2 exist. Forcing it early would
either compute a median over <3 months (disabled by design) or fabricate data
(violates the no-half-data invariant). The honest status is **OPEN until
≥2026-07-10**.

## Verification checklist — run on the first pipeline run on/after ~2026-07-10

1. **Banner shows 3/3.** Confirm the `rank_deep` active-share banner reads
   `n_matched_months=3/3` (no longer dormant).
2. **Non-null coverage across the stage-2 pool**, esp. the categories the
   tracker additions (NIFTY 50 / Bank TRI) were meant to fix:
   ```sql
   SELECT canonical_category, count(*) AS n,
          count(active_share_median_1y) AS n_as
   FROM computed_metrics m JOIN scheme_master s USING (scheme_code)
   WHERE as_of_date = '<run>'
   GROUP BY 1 ORDER BY 1;
   ```
   **Specifically verify Large Cap and Banking & Financial Services now receive
   active_share** (both were null-forever before the tracker fix).
3. **Penalty behavior (D3 [REVIEW-ALIGNED]).** Once active_share is live in a
   category pool, a fund *missing* it must draw the missing-disclosure penalty
   (not renormalize-only). Spot-check one such fund in `stage2/coverage.csv` /
   the per-category stage-2 CSV (`partial_disclosure_flag=true`,
   `missing_disclosures` contains `active_share_median_1y`).
4. **Reward-direction sanity (audit 2c).** In Large Cap, high active-share often
   signals small-cap *style drift*, not skill. If the top active-share large-caps
   are style-drifters, note it as input to C1's weight review (the stage-2
   active_share weight may need a category-aware cap or sign review) — do NOT
   change the weight here; route it through the C1 backtest like every other
   weight change.

## Outcome

No code change is expected from C4 unless step 3 (penalty wiring) or step 4
(reward direction) surfaces a defect. This note is the placeholder; replace/
extend it with the live findings after the ≥2026-07-10 run. **Until then, C4
stays open.**
