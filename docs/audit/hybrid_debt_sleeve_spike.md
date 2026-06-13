# Hybrid debt-sleeve spike — PATH A/B decision (D6, 2026-06-13)

**Decision: PATH B** — keep the T-bill debt sleeve, surface the alpha
inflation prominently in code and outputs. PATH A (real debt index) stays
open behind a bounded, operator-run network probe documented below; nothing
in this decision blocks flipping to PATH A later.

## Question

The synthetic hybrid benchmarks (`src/mfs/ingest/synthetic_hybrid.py`,
`SYNTHETIC_HYBRID_SPECS`) compound `w_debt × rf_daily` — the 91-day T-bill —
as the debt sleeve of the 65:35, 50:50 and Equity Savings hybrids. The
official NSE hybrids use the **NIFTY Composite Debt Index** (long-duration
G-sec + credit), which historically returns ~1-2%/yr above T-bills (term +
credit premium). Can a real debt index series (2013+ daily) be auto-fetched
within the invariants (no manual CSVs, no half-data)?

## What was verified (code/data level, this environment has no network)

* The equity TRI endpoint we use —
  `POST https://www.niftyindices.com/Backpage.aspx/getTotalReturnIndexString`
  (`ingest/benchmarks.py:44`) — serves the indices in `IndexMapping.json`'s
  equity universe. Our 26 live tickers all come from it (or are synthesized).
* NSE's fixed-income indices (NIFTY Composite Debt, NIFTY 10 yr Benchmark
  G-Sec, NIFTY Short Duration Debt, NIFTY 1D Rate) are published on
  niftyindices.com under a *separate* "Fixed Income" reports section, not the
  equity historical-data page this endpoint backs. Whether they appear in
  `IndexMapping.json` with a working `getTotalReturnIndexString` mapping, or
  require a different Backpage method (the WebForms UI suggests an analogous
  `get*String` method for fixed income), **cannot be established without a
  network probe** — and an unverifiable scrape must not ship (fail-fast /
  no-manual-entry invariants).
* `benchmark_daily` confirms no debt index has ever been ingested.

## Operator probe for PATH A (~30 min, network required)

1. `GET https://iislliveblob.niftyindices.com/assets/json/IndexMapping.json`
   — grep (case/whitespace-insensitive) for `Composite Debt`, `10 yr
   Benchmark G-Sec`, `Short Duration`. Record `Trading_Index_Name` /
   `Index_long_name` of any hit.
2. If present, replay the equity probe with those names:
   `uv run python - <<'EOF'` calling
   `mfs.ingest.benchmarks.fetch_tri_window('<TRADING_NAME>', '<long_name>',
   date(2024,1,1), date(2024,3,1))` — a non-empty `TotalReturnsIndex` array
   means the equity endpoint serves it directly (best case).
3. If absent/empty: open the fixed-income historical page in a browser with
   devtools, capture the XHR the "Get Data" button fires (expect another
   `Backpage.aspx/<method>` with a `cinfo` payload), and check it returns
   2013+ history in date-windowed chunks.
4. Either probe succeeding ⇒ implement PATH A:
   * add the debt ticker (+ fixed-income fetcher if the method differs) to
     `ingest/benchmarks.py`; backfill 2013+;
   * change `synthetic_hybrid.synthesize_one` callers per
     `SYNTHETIC_HYBRID_SPECS` to compose `w_debt × debt_index_daily_log_ret`
     instead of `rf_daily` for 65:35 and 50:50; Equity Savings keeps its
     arbitrage sleeve T-bill-based (benchmarks.csv:15 recipe) but moves the
     30% debt sleeve to NIFTY Short Duration Debt if fetchable;
   * keep `is_synthetic=True` (still our composition, not NSE's published
     series), update the docstring tracking-error estimate, re-synthesize
     (closes must differ from the old series), and re-run compute.
   Note: hybrid alpha/IR history then has a discontinuity at that run, like
   the D5 remap.

## PATH B (shipped now)

* **Bias statement** in `synthetic_hybrid.py`'s module docstring: one-sided
  ~35-140 bp/yr benchmark-too-easy at 35-70% debt weight (sleeve weight ×
  1-2%/yr T-bill-vs-bond-index premium). Measured hybrid beat rates
  (Balanced Advantage 100%, Aggressive Hybrid 96.7%, Equity Savings 82.6%)
  are inflated by construction.
* **Output disclosure**: `benchmark_is_synthetic` boolean on every
  stage2/stage3 `mf_report` row (`rank/shortlist.py
  with_benchmark_is_synthetic`, joined onto the stage-2 candidate frame in
  `rank_deep`) and on the D7 `passive_alternative.csv` rows, whose note
  column reads "synthetic benchmark — hybrid alpha overstated" for those
  categories.
* Tests: `tests/test_synthetic_hybrid.py` (composition math to 1e-9,
  weights-sum guard, disclosure-column truth table).

## Why PATH B is acceptable for now

The bias is one-sided and known-signed: hybrid funds look *better* than they
are vs our synthetic series, and every consumer of those rows now sees the
flag. The alternative — silently shipping a hybrid benchmark from an
unverified endpoint or a manual CSV — violates the no-manual-entry invariant.
The NIFTY 50 TRI constituents derivation (D9) does not help here: that is
the *equity* sleeve; the gap is the *debt* sleeve series.

---

## C5 close-out (Phase 7, 2026-06-13) — PATH-A probe RUN; PATH B kept

The operator probe above was executed (network, in-session). **Decision
(user, 2026-06-13): keep PATH B.** A real debt index *is* scrapeable, but no
*faithful, full-history* substitute for the recipe's named sleeves exists, so
swapping in an approximate proxy would trade a known, disclosed, one-sided bias
for a different undisclosed modelling error plus a hybrid-alpha discontinuity —
for a marginal, unverified beat-rate gain. The `benchmark_is_synthetic`
disclosure stays on every hybrid row.

### What the probe found

* **`IndexMapping.json` is reachable** and lists ~255 indices including a deep
  fixed-income family (G-Sec by tenor, AAA corporate bond, target-maturity).
* **The equity TRI endpoint does NOT serve debt indices.** Replaying
  `benchmarks.fetch_tri_window` (the `getTotalReturnIndexString` method) for
  `Nifty Composite G-sec Index`, `Nifty 10 yr Benchmark G-Sec`, and
  `NIFTY AAA Short-Term Corporate Bond` each returned **0 rows** — so PATH-A's
  best-case (equity endpoint serves it directly) is ruled out.
* **A separate fixed-income method works without auth:**
  `POST https://www.niftyindices.com/Backpage.aspx/getHistoricaldatatabletoString`
  with the same single-quoted `cinfo` payload returns
  `{"d":"[{...,\"INDEX_NAME\":...,\"HistoricalDate\":...,\"OPEN/HIGH/LOW/CLOSE\":...}]"}`.
  The `CLOSE` level is **total return** (Composite G-sec 1744→2627 over
  2018-2024 ≈ 7.0%/yr; 10yr G-Sec 1158→2436 over 2013-2024 ≈ 6.4%/yr — both
  coupon-inclusive, not clean price).

### Why no proxy cleanly substitutes

* **The recipe's exact sleeves are not published on this endpoint:** neither
  `NIFTY Composite Debt Index` (65:35 / 50:50) nor `NIFTY Short Duration Debt`
  (Equity Savings) appears in `IndexMapping.json` (searched case-insensitively).
* **`Nifty Composite G-sec Index`** (right-ish composition, diversified G-sec)
  **only starts 2018** — it cannot backfill the 2013+ span the synthetic
  hybrid TRIs (and the C1 hybrid backtest quarters from 2016) require, and is
  G-sec-only (no corporate credit).
* **`Nifty 10 yr Benchmark G-Sec`** reaches 2013 but is a single long-duration
  point (~10yr), not a composite — it injects duration volatility the official
  Composite Debt sleeve blends away, and its ~6.4%/yr CAGR is only marginally
  above the 91-day T-bill over the period, so the central beat-rate reduction
  is uncertain while the added volatility distorts alpha/IR unpredictably.

Every available option therefore (a) needs its own disclosed modelling
assumption (keep `benchmark_is_synthetic` anyway), (b) reshuffles 84 hybrid
funds, and (c) introduces an alpha/IR/capture discontinuity at the swap run —
all for an unverified improvement. PATH B (disclosed T-bill) remains the
honest choice.

### If a future operator opts into PATH A (clean single-session recipe)

1. Add a fixed-income fetch branch to `ingest/benchmarks.py` using
   `getHistoricaldatatabletoString` (NOT `getTotalReturnIndexString`); parse
   `d` → `HistoricalDate` + `CLOSE` (total-return level), chunk by ≤365d like
   the equity path.
2. Pick the sleeve: `Nifty 10 yr Benchmark G-Sec`
   (`name='NIFTY GS 10YR'`, `indexName='Nifty 10 yr Benchmark G-Sec'`) for full
   2013+ history, or `Nifty Composite G-sec Index`
   (`name='NIFTY GS COMPSITE'`, `indexName='Nifty Composite G-sec Index'`) if a
   post-2018 hybrid history is acceptable.
3. Rebuild the 65:35 / 50:50 sleeves in `ingest/synthetic_hybrid.py` to compose
   `w_debt × debt_index_daily_log_ret` instead of `rf_daily`; keep Equity
   Savings' 30% debt sleeve on the same index (Short Duration unavailable).
4. Re-synthesize, recompute, verify hybrid beat-rates fall to a realistic band,
   and KEEP `benchmark_is_synthetic=True` (still our composition, not NSE's
   published hybrid). Expect an alpha/IR discontinuity at that run (like D5).
