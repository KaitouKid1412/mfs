# Stage 2 execution state — updated 2026-06-12 ~17:30 IST

Single source of truth for resuming the Stage-2 build after any interruption.
Plan: `stage2_full_remediation.md` (this dir). Working tree may hold uncommitted
batch work — ALWAYS run the full suite before committing or launching agents.

## Committed checkpoints
- `b377355` Stage 1 (urgent fixes) — complete, verified, green close-out pipeline run
- `3e36c8e` Phase 2.0 complete (F-2, B5+ext, B6, D1, B8, D2) + operator steps done
  (departed_at + history tables applied to live DB; Motilal April healed; live
  scheme-master rebuild done — ICICI Cumulative + Dividend Yield funds admitted)
- `4479c36` Phase 2.1 batch 1 (A1-1, B14a, B1, B11, B12, F-3, F-6, C1) + operator
  steps done (213,030 zero-NAV rows deleted, chk_nav_positive VALIDATED,
  alpha_confidence + adv_unresolved_pct columns applied via `mfs db init`)

## Uncommitted in working tree (verify suite green, then commit as "Phase 2.1 batches 2-4")
- A1-2 signed alpha + alpha_confidence (DONE, agent-verified green)
- A1-3/A1-5/A1-9/A1-12 orchestrator cluster (DONE, agent-verified 991 green)
- F-7/F-8/F-9 CI + fixtures + mypy (DONE, agent-verified; CI selection 598 green)
- B2/B3/B13 ingest gates (agent killed by session limit AFTER ~49 tool uses —
  code present: statement-date coverage, weight-sum gates, shrinkage guard at
  holdings/_run.py:157; needs verification, no agent report)
- A1-4/A1-6/A1-8 + orchestrator mypy (agent 4a killed mid-flight — epoch
  unification in returns/rolling, Sortino/IR pins, active_share ISIN; VERIFY:
  golden-pin changes in tests/test_metrics_math.py need review)
- A1-7/A1-11/C4/B4 (agent 4b killed mid-flight — aum_impact rewrite + ADV guard
  threaded, C4 same-transaction flr refresh, B4 partial-day rejection; VERIFY:
  writers.py transaction shape + amfi_nav threshold)

## Remaining work (in order)
1. CHECKPOINT: full suite -> fix stragglers -> commit batches 2-4.
2. Ingest batch 3: B9 (--full re-download), B14b (PTR MoM tripwire,
   managers/_run.py), B15 (Gate B ERROR status, coverage.py), C7 stray
   (invariant-series caching, alignment.py).
3. Ingest refactor chain (sequential, same files): F-4 -> F-5 -> C2 -> C3, C5.
4. Phase 2.2: A1-10 coordinated recompute. Operator-led: `uv run mfs db init`
   (any pending columns), then full recompute at current as_of, verification
   battery from the A1-10 spec (signed-alpha distribution: min<0 expected;
   STALE counts; adv_unresolved_pct nulls), DELETE archived 2026-06-08
   partition. A2-4's penalty calibration query runs on this partition.
5. Phase 2.3 (strictly sequential): A2-1 -> A2-2 -> A2-3 -> A2-4 -> A2-5 ->
   A2-6 -> A2-7 -> A2-8 -> A2-10 -> A2-11 -> A2-9 (pinned e2e last), + B7, D9.
   Also: add alpha_confidence to STAGE1_OUTPUT_COLS in shortlist.py (A1-2
   follow-up the compute agent couldn't do — rank/ was off-limits).
6. Phase 2.4 (parallel): D3-D8, D10 / E1-E7 / F-10..F-15, F-14.

## Process rules learned (enforce in every agent prompt)
- File-ownership lists per concurrent agent; one file = one owner.
- "FULL suite AT MOST ONCE, then REPORT IMMEDIATELY" (verify-loop pathology).
- Agents killed by session limits usually leave complete work on disk —
  verify with greps + suite before re-running anything.
- pytest/git/psql are token-free; never spawn an agent for verification.
- Session limits: check remaining budget before launching parallel batches;
  prefer one bigger sequential agent over three parallel ones when budget is low.

## Live-DB migration state (applied)
departed_at, scheme_master_history, rank_history, chk_nav_positive (VALIDATED),
alpha_confidence, adv_unresolved_pct (verify with `mfs db init` — idempotent).
