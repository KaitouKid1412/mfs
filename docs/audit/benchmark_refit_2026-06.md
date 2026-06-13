# Benchmark refit evidence — Value & Energy remap (D5, 2026-06-13)

Decision evidence for the `configs/benchmarks.csv` changes shipped with D5.
Produced by `tools/benchmark_fit.py` (D4) against the live DB, read-only, at
`computed_metrics` as_of **2026-06-12** (latest partition; signed-alpha stack,
post A1-10 recompute). The refit runs the *production* rolling-3y regression
(`compute.alpha.rolling_alpha_beta_r2` over `compute.alignment.align_scheme`)
per fund per candidate ticker and reports the median across the category's
funds.

Pre-registered selection rule (spec D5): adopt the candidate with the highest
median R², only if it reaches **>= 0.75**; otherwise keep the incumbent and
rely on the D4 `benchmark_fit_low_confidence` flag.

## Baseline (report mode, as_of 2026-06-12)

Five categories classify LOW_CONFIDENCE_ALPHA (median rolling-3y R² < 0.80):

| category        | n  | median R² | median beta |
|-----------------|----|-----------|-------------|
| Energy          | 4  | 0.648     | 0.674       |
| Value           | 20 | 0.739     | 0.576       |
| MNC             | 5  | 0.774     | 0.750       |
| Equity Savings  | 24 | 0.775     | 1.027       |
| Infrastructure  | 16 | 0.780     | 0.885       |

(Large Cap for contrast: 0.944/0.942, n=31.)

## Value — refit

    uv run python tools/benchmark_fit.py refit --category Value \
        --candidates 'NIFTY 500 TRI,NIFTY 500 Value 50 TRI'

| candidate              | available | n_regressable | median R² | median beta | median alpha (ann) |
|------------------------|-----------|---------------|-----------|-------------|--------------------|
| NIFTY 500 TRI          | yes       | 20            | **0.8969**| 0.9459      | +0.0412            |
| NIFTY 500 Value 50 TRI | yes (incumbent) | 20      | 0.7387    | 0.5759      | -0.0061            |

**Decision: Value -> NIFTY 500 TRI** (0.8969 >= 0.75; +0.158 R² over the
incumbent; beta normalizes 0.58 -> 0.95). The old factor-index mapping was a
mis-specification: Indian "Value" funds hold broad-market value-tilted books,
not the mechanical NIFTY 500 Value 50 factor portfolio — beta 0.58 against it
meant ~42% of fund variance landed in the intercept, which the audit caught
as "0/19 funds beat the index, yet all alphas positive". SEBI SIDs for these
funds overwhelmingly benchmark NIFTY 500 / BSE 500 TRI, consistent with this
result. Note the sign flip: median annualized alpha vs the correct benchmark
is +4.1%, vs -0.6% against the misfit factor index.

## Energy — refit

    uv run python tools/benchmark_fit.py refit --category Energy \
        --candidates 'NIFTY Energy TRI,NIFTY Infrastructure TRI,NIFTY 500 TRI,NIFTY Commodities TRI'

| candidate                | available | n_regressable | median R² | median beta | median alpha (ann) |
|--------------------------|-----------|---------------|-----------|-------------|--------------------|
| NIFTY Infrastructure TRI | yes       | 4             | **0.8119**| 0.9266      | +0.0541            |
| NIFTY 500 TRI            | yes       | 4             | 0.7808    | 1.0317      | +0.0831            |
| NIFTY Energy TRI         | yes (incumbent) | 4       | 0.6478    | 0.6742      | +0.1227            |
| NIFTY Commodities TRI    | **no — not in benchmark_daily** | 0 | —  | —          | —                  |

**Decision: Energy -> NIFTY Infrastructure TRI** (0.8119 >= 0.75; +0.164 R²
over the incumbent; beta 0.67 -> 0.93). The category's funds (natural
resources / energy-opportunities mandates) hold diversified
resources+industrials books, not the RIL/NTPC/ONGC-heavy NIFTY Energy index.
Alpha halves (+12.3% -> +5.4% median annualized) — the incumbent's "alpha"
was largely unexplained fit residual.

**Open follow-up:** the spec's preferred candidate NIFTY Commodities TRI could
not be scored — it has never been ingested. It is now in
`ingest.benchmarks.NSE_TRI_MAP` ("NIFTY COMMODITIES" / "Nifty Commodities");
after the next benchmark ingest (network step), re-run:

    uv run mfs ingest benchmarks            # picks up the new ticker, full 2013+ backfill
    uv run python tools/benchmark_fit.py refit --category Energy \
        --candidates 'NIFTY Infrastructure TRI,NIFTY Commodities TRI'

If Commodities' median R² beats 0.812, remap again (second dated comment row
in benchmarks.csv; second discontinuity is acceptable for n=4 funds).

## Side effects handled in this change

* **Constituents**: both new tickers already have derivation specs
  (`ingest/constituents/derive.py` `_SPECS`: `nifty_500_tri`,
  `nifty_infrastructure_tri`) and live `index_constituents_monthly` rows
  (2 months each, verified 2026-06-13) — no `_ETF_TRACKERS` addition and no
  `accepted_missing` entry needed; no phantom Gate B gap (the entity set
  derives from `scheme_master.benchmark_ticker`).
* **benchmarks.csv comment row**: dated remap note added;
  `master/benchmark_map.py` now reads with `comment_prefix="#"`, and the
  csv.DictReader path in `ingest/constituents/_run.py:discover_tickers`
  skips `#` rows.
* **History discontinuity**: alpha/IR/capture/R²/beta for Value and Energy
  are measured against the OLD tickers in every `computed_metrics` /
  `rank_history` partition up to and including 2026-06-12, and against the
  new ones from the first post-remap pipeline run. Cross-run comparisons for
  these two categories straddling that date are not like-for-like.

## Operator steps (in order, next pipeline run does most of this)

1. `uv run mfs ingest benchmarks` — backfills NIFTY Commodities TRI 2013+
   (the only genuinely new fetch; the two adopted tickers are already
   ingested daily). If it returns 0 rows for Commodities, verify the
   IndexMapping names per the NSE_TRI_MAP comment.
2. `uv run mfs build scheme-master` — re-stamps `benchmark_ticker` for the
   ~24 Value/Energy funds (the pipeline does this every run anyway).
3. Next compute run re-derives metrics on the new mappings; verify
   `uv run python tools/benchmark_fit.py report` shows Value >= ~0.85 and
   Energy >= ~0.80 (or still flagged low-confidence by D4 if not).
4. Re-run the Energy refit vs NIFTY Commodities TRI (command above).

---

## C3 close-out (Phase 7, 2026-06-13) — Commodities re-fit + low-confidence review

NIFTY Commodities TRI is now ingested (2013-01-01 .. 2026-06-12, 3329 rows).
Re-ran the Energy refit and reviewed every category still flagged
`benchmark_fit_low_confidence` at `computed_metrics` as_of **2026-06-13**.
**Decision: NO further remaps** — each flagged category keeps its incumbent
benchmark with a documented acceptance (no candidate is a *clearly* better fit,
and where one is marginally higher on raw R² the beta evidence makes it
structurally wrong). Energy is no longer flagged (0.812 ≥ 0.80).

### Energy — Commodities re-fit (the D5 open follow-up)

    uv run python tools/benchmark_fit.py refit --category Energy \
        --candidates 'NIFTY Infrastructure TRI,NIFTY Commodities TRI,NIFTY Energy TRI,NIFTY 500 TRI'

| candidate                | n_regressable | median R² | median beta | median alpha (ann) |
|--------------------------|---------------|-----------|-------------|--------------------|
| NIFTY Infrastructure TRI | 4             | **0.8119**| 0.927       | +0.054             |
| NIFTY 500 TRI            | 4             | 0.7810    | 1.032       | +0.083             |
| NIFTY Commodities TRI    | 4             | 0.7701    | 0.813       | +0.094             |
| NIFTY Energy TRI         | 4             | 0.6479    | 0.674       | +0.122             |

**Outcome: keep Energy → NIFTY Infrastructure TRI.** Commodities TRI (0.770)
did NOT beat the D5-adopted Infrastructure TRI (0.812), so the D5 mapping
stands and the spec's "remap again if Commodities beats 0.812" condition is not
met. Energy now sits at 0.812 and is no longer low-confidence.

### Remaining low-confidence categories (median R² < 0.80) — all accepted

Report at as_of 2026-06-13 flagged three: MNC (0.774), Equity Savings (0.775),
Infrastructure (0.780). Refit evidence below.

**MNC — keep NIFTY MNC TRI (documented acceptance).**

| candidate               | median R² | median beta |
|-------------------------|-----------|-------------|
| NIFTY MNC TRI (incumbent)| **0.774**| 0.750       |
| NIFTY 500 TRI           | 0.714     | 0.672       |
| NIFTY India Consumption TRI | 0.663 | 0.674       |
| NIFTY 100 TRI           | 0.646     | 0.657       |

The incumbent is the best available fit and the purpose-built index; every
broad/thematic alternative is materially worse. MNC is a genuinely niche
multinational-tilted category (n=5–7) with no better-fitting published index.
Accept the residual low-confidence flag.

**Infrastructure — keep NIFTY Infrastructure TRI (documented acceptance).**

| candidate                  | median R² | median beta |
|----------------------------|-----------|-------------|
| NIFTY Infrastructure TRI (incumbent)| **0.7805**| 0.885 |
| NIFTY 500 TRI              | 0.7800    | 1.025       |
| NIFTY India Manufacturing TRI | 0.7691 | 0.963       |
| NIFTY Commodities TRI      | 0.7540    | 0.751       |
| NIFTY PSE TRI              | 0.6780    | 0.611       |

Incumbent is (marginally) the best fit. NIFTY 500 TRI ties on R² (0.780) but a
broad-market remap would strip the thematic meaning the category exists to
express and push beta to ~1.0. No *clearly* better fit; accept the flag.

**Equity Savings — keep NIFTY Equity Savings TRI (documented acceptance).**

| candidate                 | median R² | median beta |
|---------------------------|-----------|-------------|
| NIFTY 100 TRI             | 0.8005    | 0.306       |
| NIFTY 50 Hybrid 50:50 TRI | 0.7754    | 0.616       |
| NIFTY 50 Hybrid 65:35 TRI | 0.7754    | 0.474       |
| NIFTY Equity Savings TRI (incumbent)| 0.7754| 1.027 |

This is the one case where the mechanical "highest R²" rule and the right
answer diverge. NIFTY 100 TRI scores marginally higher (0.801 vs 0.775) **but
its beta is 0.31** — i.e. it regresses the fund's ~30% net-equity sleeve and
dumps the entire debt + arbitrage sleeve return into the intercept. Alpha
measured that way would systematically report the debt/arbitrage *yield* as
manager "skill" — the inverse of the Value mis-fit the audit caught. The NIFTY
Equity Savings TRI already embeds the ~35/30/35 equity/arbitrage/debt structure,
so alpha against it is true selection skill; its lower R² reflects per-fund
variation in that mix, not a wrong index. Keep the incumbent; accept the flag.
(If a future operator prefers raw fit over structural correctness, NIFTY 100 TRI
is the documented alternative.)

### Acceptance

`benchmark_fit.py report` at 2026-06-13 shows no *un-reviewed* low-confidence
category: Energy resolved (remapped in D5, Commodities ruled out here), and
MNC / Infrastructure / Equity Savings each carry a documented acceptance above.
No `configs/benchmarks.csv` ticker changes in this close-out (comment row only),
so no scheme-master rebuild or recompute was required and there is no new
history discontinuity.
