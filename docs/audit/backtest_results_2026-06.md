# Retro-IC backtest results — composite-weight validation (C1, 2026-06)

**Verdict: REFUTED.** The Stage-1 composite, ranked on trailing NAV-only
metrics, does **not** predict forward 1-year category-relative returns on the
survivor universe. Pooled 1y mean Spearman IC = **−0.0503** (negative), so the
pre-registered REFUTED condition (`mean IC ≤ 0`) fires regardless of the t-stat.

> ⚠️ **SURVIVORSHIP-BIASED — these ICs are an UPPER BOUND.** The universe is the
> *current* `scheme_master` actives; funds that died or merged since 2016 are
> unrecoverable and absent (forward-only containment, by user decision). Dead
> funds were disproportionately *poor* performers, so excluding them inflates
> measured persistence. The real-time IC is **lower** than every number here.
> A negative *upper-bound* IC is therefore a robust negative signal: the true
> predictive value is no better, and almost certainly worse.

Run: `uv run python tools/backtest_ic.py --since 2016-01-01 --horizons 1y,3y`
(step `1w`), 2026-06-13. Universe 680 funds, 27 categories, 37 quarter-ends
(2016-03-31 .. 2025-03-31). Benchmarks are the **final** post-D5 + post-C3
mappings (Value→NIFTY 500 TRI, Energy→NIFTY Infrastructure TRI; C3 confirmed no
further remaps). Metrics are recomputed point-in-time against the *current*
benchmark for every historical quarter, so there is no straddle/discontinuity
issue here (unlike the production `computed_metrics` partitions). Artifacts:
`data/output/backtest/2026-06-13/{ic_by_quarter,ic_summary,sensitivity}.csv`
(git-ignored).

## Pre-registered thresholds (from the tool docstring)

| verdict | condition (1y pooled) |
|---------|-----------------------|
| JUSTIFIED | mean IC ≥ 0.05 AND \|t\| ≥ 2 AND top-5 spread > 0 |
| REFUTED | mean IC ≤ 0 OR \|t\| < 1 |
| INCONCLUSIVE | otherwise (keep weights, label outputs 'unvalidated') |

These were registered *before* the full run (the smoke run only saw Large/Mid Cap).

## Pooled results

| horizon | mean IC | t (non-overlapping)¹ | t (overlapping)² | top-5 spread | verdict |
|---------|---------|----------------------|------------------|--------------|---------|
| **1y** | **−0.0503** | −1.16 (n=9) | −3.12 (n=33) | −0.0033 | **REFUTED** |
| 3y | −0.0516 | −0.92 (n=3) | −3.06 (n=25) | −0.0071 | REFUTED |

¹ Statistically clean: one observation per non-overlapping forward window
(every 4th quarter at 1y, every 12th at 3y). This is the honest significance.
² Autocorrelation-inflated: consecutive quarterly 1y windows overlap by 9
months, so the overlapping t overstates significance — reported only with this
caveat. **The REFUTED call rests on the negative mean IC, not the t-stat.**

## Per-category IC (the actionable breakdown)

Sorted by 1y mean IC. The composite **helps** (positive IC) in a handful of
categories and **hurts** (negative IC) in most equity categories.

| category | 1y mean IC | 1y t(non-ov) | 1y top-5 spread | 3y mean IC | n_q |
|----------|-----------:|-------------:|----------------:|-----------:|----:|
| Equity Savings | **+0.216** | +2.44 | +0.0043 | **+0.418** | 28 |
| Balanced Advantage | **+0.117** | +0.44 | −0.0001 | +0.107 | 30 |
| Large & Mid Cap | +0.057 | +0.37 | −0.0005 | +0.090 | 33 |
| Banking & Financial Services | +0.010 | +1.16 | +0.0059 | +0.065 | 33 |
| Small Cap | +0.007 | +0.31 | −0.0032 | +0.037 | 33 |
| ELSS | −0.021 | −0.70 | +0.0100 | +0.004 | 33 |
| Aggressive Hybrid | −0.037 | +0.03 | −0.0022 | −0.057 | 33 |
| Large Cap | −0.061 | −0.54 | −0.0142 | −0.054 | 33 |
| Infrastructure | −0.073 | −0.55 | +0.0007 | −0.179 | 33 |
| Value | −0.077 | −0.14 | +0.0068 | +0.080 | 33 |
| Multi Cap | −0.087 | — | — | −0.659³ | 5 |
| Flexi Cap | −0.101 | −0.81 | −0.0060 | −0.115 | 33 |
| Pharma & Healthcare | −0.111 | −2.21 | −0.0001 | −0.169 | 14 |
| Consumption | −0.115 | −0.82 | −0.0027 | −0.172 | 33 |
| Thematic | −0.136 | −0.46 | −0.0072 | −0.210 | 33 |
| Focused | **−0.197** | −2.78 | −0.0195 | −0.303 | 33 |
| Mid Cap | **−0.204** | −3.03 | −0.0208 | −0.308 | 33 |
| ESG | −0.214³ | −8.33 | −0.0042 | — | 5 |

³ Multi Cap (n=5q) and ESG (n=5q) have too few quarters for a reliable read —
flagged, not relied on.

**Reading it:**
- **Where the ranking works:** the *hybrid* categories — Equity Savings
  (+0.216, the only category clearing the JUSTIFIED IC bar with a clean
  \|t\|>2) and Balanced Advantage (+0.117). Plausible mechanism: in
  lower-volatility blended funds, trailing risk-adjusted metrics (Sortino,
  capture) capture a persistent skill/cost edge that mean-reversion doesn't
  wash out. Large & Mid Cap and BFSI are weakly positive.
- **Where it actively misleads:** the *concentrated / high-dispersion* equity
  categories — **Mid Cap (−0.204), Focused (−0.197)**, Thematic, Consumption,
  Pharma, Flexi Cap. In these, last-3y winners mean-revert: ranking on trailing
  performance is worse than a coin flip at 1y, and the effect is monotone and
  significant at 3y (Mid Cap −0.308, Focused −0.303). The top-5 forward spread
  is also negative there (the top-5 picks *underperform* the category median).

## Sensitivity (each Stage-1 weight ±50% renormalized, + p25_collapsed)

511 category-quarter cells, 1y horizon. Δ vs the baseline composite IC.

| variant | mean IC | Δ vs baseline | top-5 overlap |
|---------|--------:|--------------:|--------------:|
| **p25_collapsed** | **−0.0428** | **+0.0057** | 0.968 |
| ret_5y_median +50% | −0.0455 | +0.0030 | 0.985 |
| alpha_3y −50% | −0.0458 | +0.0027 | 0.980 |
| ret_5y_p25 +50% | −0.0472 | +0.0013 | 0.987 |
| sortino_3y −50% | −0.0474 | +0.0011 | 0.988 |
| ret_3y_p25 −50% | −0.0479 | +0.0006 | 0.987 |
| info_ratio_3y −50% | −0.0480 | +0.0005 | 0.986 |
| ret_3y_median −50% | −0.0481 | +0.0004 | 0.982 |
| capture_efficiency −50% | −0.0482 | +0.0003 | 0.993 |
| ret_5y_p25 −50% | −0.0483 | +0.0002 | 0.987 |
| ret_3y_p25 +50% | −0.0484 | +0.0001 | 0.989 |
| alpha_3y +50% | −0.0483 | +0.0002 | 0.986 |
| ret_3y_median +50% | −0.0488 | −0.0003 | 0.991 |
| info_ratio_3y +50% | −0.0491 | −0.0006 | 0.991 |
| sortino_3y +50% | −0.0487 | −0.0002 | 0.992 |
| ret_5y_median −50% | −0.0498 | −0.0013 | 0.985 |
| capture_efficiency +50% | −0.0502 | −0.0017 | 0.993 |
| baseline | −0.0485⁴ | 0 | 1.000 |

⁴ Sensitivity baseline (−0.0485, mean over the 511 cells) differs slightly from
the pooled −0.0503 (per-quarter category-equal-weight then pooled); both are
the same sign and magnitude — the difference is the aggregation order.

**Reading it:**
- **`p25_collapsed` is the single best variant** (+0.0057): zeroing both p25
  weights and folding them into the 3y/5y medians. Consistent with the medians
  carrying more signal than the downside-tail (p25) returns, and with the
  config author's note at `pipeline.yaml:113` anticipating this candidate.
- **Reducing alpha helps; increasing it hurts** (alpha_3y −50% = +0.0027, +50% =
  −0.0002). Alpha is the single heaviest input (0.25) and is mildly
  anti-predictive — corroborating the audit's concern that in misfit-benchmark
  categories "alpha" is unexplained residual, not skill.
- **`capture_efficiency` is the most anti-predictive** of the small weights
  (+50% = −0.0017, the worst single move).
- **Crucially, NO single-weight perturbation makes the composite predictive.**
  Every variant remains negative-IC. Reweighting within this metric set cannot
  rescue forward predictiveness on the survivor universe; the ceiling is a less
  *negative* IC, not a positive one.

## Proposed weight changes — NOT APPLIED (awaiting user approval)

Per the locked decision (report-and-stop), **no `configs/pipeline.yaml` weight
has been changed.** The following are evidence-based proposals for the user to
approve or reject as a separate task.

### Proposal A (primary, directly tested) — adopt `p25_collapsed`

The only registered multi-weight variant, and the best performer (+0.0057 IC).
Diff against `composite_weights_stage1`:

```diff
 composite_weights_stage1:
-  ret_3y_median: 0.15
-  ret_3y_p25:    0.10
-  ret_5y_median: 0.15
-  ret_5y_p25:    0.10
+  ret_3y_median: 0.25
+  ret_3y_p25:    0.00
+  ret_5y_median: 0.25
+  ret_5y_p25:    0.00
   alpha_3y:      0.25
   sortino_3y:    0.10
   info_ratio_3y: 0.10
   capture_efficiency: 0.05   # sum stays 1.00
```

Mirror in `composite_weights_stage2` (NOT backtested — Phase-2 metrics have no
pre-2026 history; apply only for consistency, sum stays 0.85):

```diff
 composite_weights_stage2:
-  ret_3y_median: 0.10
-  ret_3y_p25:    0.07
-  ret_5y_median: 0.10
-  ret_5y_p25:    0.07
+  ret_3y_median: 0.17
+  ret_3y_p25:    0.00
+  ret_5y_median: 0.17
+  ret_5y_p25:    0.00
   ...
```

### Proposal B (optional, single-lever evidence) — also halve `alpha_3y`

`alpha_3y −50%` improved IC by +0.0027 and alpha is the heaviest, most
anti-predictive input. **Caveat:** the backtest perturbed one weight at a time;
combining B with A is an extrapolation, not a directly validated configuration.
If adopted, redistribute the freed 0.125 across the two medians (→0.3125 each)
to keep the sum at 1.00, and **re-run the backtest on the combined vector
before shipping** rather than trusting the additive estimate.

### Proposal C (the honest meta-recommendation) — label + scope

Even Proposal A leaves the pooled IC negative. Per the REFUTED handling in the
tool docstring ("label outputs 'unvalidated'"), recommend:
1. Mark the composite ranking **"not validated as forward-predictive"** in
   investor-facing outputs (it remains a defensible *descriptive* quality
   screen — low-cost, consistent, risk-adjusted — not a performance forecast).
2. Add a per-category caveat flagging **Mid Cap, Focused, Thematic, Consumption,
   Pharma & Healthcare** as categories where trailing-metric ranking is
   *anti-predictive* on survivors (top picks mean-revert). Consider returns-only
   or equal-weight for these pending a redesign.
3. Note the bright spot: **Equity Savings / Balanced Advantage** rankings *are*
   forward-predictive (Equity Savings clears the JUSTIFIED IC bar) — the
   composite earns its keep in the hybrid sleeve.

A genuine fix (not a reweighting) would require a different signal family
(e.g. contrarian / mean-reversion-aware, or shorter-horizon momentum) and is
out of scope for this report.

## STOP

No weights changed. The proposed `composite_weights_stage1/stage2` diffs above
require user approval. Approving Proposal B additionally requires a confirming
backtest of the combined vector before it ships.
