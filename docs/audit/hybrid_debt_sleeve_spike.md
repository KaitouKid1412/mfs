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
