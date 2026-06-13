# Phase 7 — Low / medium priority plan (debt reduction & hygiene)

**For the implementing session (Claude Opus 4.8).** These are the remaining
audit items that do **not** change rankings — code-debt reduction, test
coverage, operational hygiene, and documented accepted-risks. Critical items
(things that affect ranking correctness/confidence) are in the companion
`docs/phase7/critical_issues_plan.md`; do those first if both are queued.

## Before you start — read these
- `docs/phase6/EXECUTION_STATE.md` — Stage-2 state + operator follow-ups.
- `docs/audit/2026-06-10_pipeline_audit.md` — the original audit (the "why").

## Ground rules (same as the critical plan)
- `pytest`/`git`/`psql`/`uv run` all work in-session, no agent needed to verify.
- One item = one commit; full suite green first (`uv run pytest tests/ -q --tb=line -p no:warnings; echo pytest_exit=$?` — read the printed exit code).
- Apply any DB migration immediately after its commit via `uv run mfs db init`.
- Invariants: fail-fast on stale/partial data; no manual data entry; AUM AMFI-only; incremental-by-default.
- Commit-message footer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Don't push unless asked.

## User decisions already locked (2026-06-13)
- **Survivorship:** accept forward-only + document (L7 below is a doc task, NOT a backfill).
- **Reverse-engineered adapters:** keep + document, revisit on breakage (L6 below).

These items are independent — they can be done in any order, and most are S (<1h).

---

## L1 — Migrate the remaining manager adapters to the shared PTR helper

**What:** Stage-2 built `parse_ptr_pages()` in `src/mfs/ingest/_common.py` and
migrated 3 of ~40 manager adapters as proof. The other ~37 still copy-paste the
~25-line PTR page-loop skeleton (audit dim 5: "Manager-adapter family
duplicates a near-identical parse skeleton ~40×").

**Why:** Pure debt reduction. Each duplicated skeleton is a place a future fix
has to be applied 40 times; the unit-convention bug class lives here. No
behavior change — adapter #85 should cost ~10 lines, not 150.

**How:**
1. For each remaining manager adapter in `src/mfs/ingest/managers/` that still
   hand-rolls the PTR page loop, replace it with a call to `_common.parse_ptr_pages(...)`
   (the 3 migrated adapters — hdfc, canara_robeco, iti — are the templates).
2. Where an adapter's PTR is a pure text/regex extraction with no geometry, use
   the declarative `SimplePtrSpec` shape documented in `_common.py`.
3. Each adapter has a fixture test (committed extracts or local PDFs) — they must
   stay green **unchanged** (the migration is behavior-preserving). Run the
   per-adapter test after each migration.
4. Do this in small batches (5-8 adapters/commit) so a regression is easy to bisect.

**Acceptance:** `grep -rc 'parse_ptr_pages' src/mfs/ingest/managers/ | grep -v ':0'`
covers the simple adapters; the bespoke-geometry ones that genuinely can't use
the helper are documented inline. All adapter tests green.

---

## L2 — Widen holdings adapter test coverage

**What:** Only 3 of 46 holdings adapters have parse tests (audit dim 7). Stage 2
added runtime tripwires (statement-date validation, weight-sum gate, unit-flip
guard, collision resolution) that compensate, but frozen-fixture value pins are thin.

**Why:** The runtime gates catch *gross* drift; exact value pins catch *subtle*
parse regressions (a shifted column, a renamed sheet). Higher-AUM AMCs deserve pins.

**How:**
1. Use the F-8 pattern: `tools/make_fixture_pdf.py` (and the analogous slim-Excel
   approach) to commit minimal extracts under `tests/fixtures/` for the highest-AUM
   holdings AMCs not yet covered.
2. Add value-pin tests (exact holding count + a couple of top weights/ISINs)
   marked appropriately so CI runs the ones with committed fixtures and skips
   the `local_data` ones.
3. Prioritise by AUM/coverage impact, not alphabetically.

**Acceptance:** holdings adapter pin coverage materially up from 3/46 (target the
top ~15 by AUM); the deselection-count tripwire in CI updated if the `local_data`
set changes.

---

## L3 — ISIN → base_fund_id uniqueness assertion

**What:** scheme_master can carry duplicate growth-ISINs across base_fund_ids
(Nippon segregated/bonus variants — audit 1a, low). Stage-2's dedupe-before-z
(A2-3) already removed the *ranking* impact, but there's no hard tripwire on the
underlying data.

**Why:** Cheap defence-in-depth. A duplicate growth-ISIN means two base_fund_ids
claim the same fund — a data-model violation that should fail loud, not be
silently de-duped downstream.

**How:** In `src/mfs/master/scheme_master.py` build (post-sync), assert that
`isin_growth` is unique among active DIRECT+GROWTH rows; on violation, log the
offending pairs and either raise (fail-fast) or flag — match the existing
category-relabel guard's severity convention (F-12). Add a unit test with a
duplicate-ISIN fixture.

**Acceptance:** a synthetic duplicate-growth-ISIN frame trips the assertion;
the live build still passes (Nippon variants differ by plan/option, so the
*growth* ISIN should be unique — confirm with read-only psql first).

---

## L4 — Advisory lock on standalone ingest/compute CLI commands

**What:** The Postgres advisory lock guards `mfs pipeline` only; standalone
`mfs ingest …` / `mfs compute …` can interleave with a running pipeline
(audit dim 8, low).

**Why:** Transactional writes bound the damage, but two concurrent writers to the
same partition is still a footgun for an operator running an ad-hoc ingest during
a pipeline run.

**How:** Wrap the standalone ingest/compute CLI commands in `src/mfs/cli.py` with
the same `connection.pipeline_lock()` context the pipeline uses (or a clearly
named shared lock). A blocked command should print the same "another run holds
the lock" message and exit cleanly. Add a test that a second lock acquisition is refused.

**Acceptance:** running a standalone ingest while the lock is held exits cleanly
with the contention message; full suite green.

---

## L5 — Schedule backups (operator-assisted)

**What:** Stage 2 wrote `scripts/backup.sh` (pg_dump -Fc + data/raw archive +
restore-test step) and `docs/ops/backups.md`, but nothing is scheduled. The
critic flagged 9.6 GB of partially irreplaceable data on one machine with no
recurring backup.

**Why:** AMC sites delist old monthly portfolios; `index_constituents_monthly`
and 12,874 dead-fund NAV histories live only in this DB. Loss of the machine =
unrecoverable.

**How:**
1. The code is done. Wire `scripts/backup.sh` into cron or launchd per the
   runbook (`docs/ops/backups.md`).
2. Run the restore-test step once to prove the dump is good.
3. **NEEDS THE USER:** off-site/cloud destination + credentials (a local-only
   backup doesn't survive disk loss). Surface the choice (S3 / rclone / external
   disk) and wire the chosen target into `backup.sh`'s env.

**Acceptance:** a scheduled job exists, one successful backup + restore-test
recorded; off-site target configured (pending the user's credential choice).

---

## L6 — Keep the scraping-provenance inventory current (no removal)

**What:** Reverse-engineered access paths (ITI AES key, Edelweiss bot-evasion,
Kotak prodtest host, jio_blackrock action-id) are per-AMC SPOFs with ToS
exposure (audit 1b + critic). **User decision: keep + document, revisit on
breakage** — do NOT remove or replace them now.

**Why:** These adapters work today; pre-emptive removal loses coverage. The risk
is managed by documentation + a revisit-on-breakage trigger.

**How (this is a small doc/process task, not a code change):**
1. Ensure `docs/ops/scraping_provenance.md` lists every reverse-engineered path
   with: what breaks if the AMC rotates it, the ToS posture, and the agreed
   action = **accept-risk** (per the user's decision).
2. Add a one-line runbook entry: when one of these adapters fails in a pipeline
   run (it'll surface as a per-AMC `*.amc_failed` event, isolated), the operator
   re-derives the access path or drops that AMC for the month — fail-fast already
   keeps the rest of the run clean.
3. If a Kotak production (non-`prodtest`) host is derivable from the code/docs
   **without** new reverse-engineering, switching to it is an optional safe win
   (F-13) — otherwise leave it and document.

**Acceptance:** the memo is complete and current; no adapter code removed.

**Needs the user:** nothing further — the accept-risk posture is already chosen.
Surface to the user only if an AMC's access actually breaks.

---

## L7 — Document the survivorship caveat everywhere it matters

**What:** **User decision: accept forward-only + document.** Diff-sync keeps
future departures (`is_active=false`); pre-2026-06 dead funds remain absent and
won't be backfilled.

**Why:** The bias is real (beat-rates and backtest ICs are survivor-only and
optimistic). The mitigation is honesty: every artifact that reports historical
performance must say so, so no one mistakes a survivor-only number for the
full-universe truth.

**How (doc-only):**
1. Add a one-line survivor-only caveat to: the README's "known limitations", the
   `passive_alternative.csv` header (it may already be there — verify), the
   investor `REPORT.md` template, and the backtest report template (C1 already
   requires it).
2. Confirm `departed_at` + the diff-sync are described in the README so a reader
   knows containment is forward-only by design.

**Acceptance:** grep shows the survivor-only caveat in README, passive CSV
header, REPORT.md, and backtest report; no code change.

---

## L8 — Minimum-cohort handling for tiny categories (depends on C1)

**What:** Six categories rank on 1-4 funds where z-scores are noise (audit dim
6). Stage 2 mitigated at the display layer (cohort-size columns + "index wins"
verdicts). A real guard (suppress ranking / force the index recommendation below
N funds) is still missing.

**Why:** Ranking 5 funds out of 1-4 is statistically meaningless; the display
context helps but doesn't stop a user treating a degenerate FMCG "rank 1" as signal.

**How:**
1. **Do C1 (backtest) first** — it tells you per-category whether ranking is
   reliable at small N, which sets the threshold on evidence rather than a guess.
2. Implement a `min_cohort_for_ranking` config knob: below it, the category's
   output is emitted with an explicit "insufficient peer set — consider the index
   fund/ETF" banner instead of a confident top-5.
3. **NEEDS THE USER:** confirm the threshold N (a product call) once C1's
   evidence is in. Default proposal: N where the backtest IC stops being
   meaningful (often n≥8, the tool's own IC floor).

**Acceptance:** tiny categories carry the insufficient-cohort banner; threshold
documented with backtest justification. (Blocked on C1 + a one-line user confirmation.)

---

## L9 — ret_median / ret_p25 weight collapse (depends on C1)

**What:** ret_median and ret_p25 correlate 0.92-0.97 (audit 2c); the nominal
0.15/0.10 splits are false precision and the real trailing-performance load is
~0.75. Stage 2 documented this in `pipeline.yaml`; the `p25_collapsed` variant
exists in the backtest tool.

**Why:** Collapsing the near-duplicate pair into one weight removes false
precision and frees weight budget for genuinely independent signals (TER, IR).

**How:** This is a **weight change**, so it goes through C1's report-and-approve
gate. After C1, if the `p25_collapsed` sensitivity variant matches or beats
baseline IC, propose the collapse in the C1 weight-change diff for user approval.
Do not change weights here directly.

**Acceptance:** handled inside C1's proposal; no standalone commit.

---

## Items explicitly accepted as-is (no action — listed for completeness)
- **T-bill cold-start ~10.5h:** deliberate 4-worker politeness against RBI; one-time cost. Documented in `EXECUTION_STATE.md`. No change unless you accept higher bot-risk.
- **Index constituents derived from tracker holdings:** documented approximation; fix only by buying an official NSE constituents feed.
- **Min-investment / closed-to-inflow metadata:** no clean machine-readable source; would require manual entry (invariant violation) or a vendor feed. Out of scope until a source exists.

## Suggested order
L7 (doc, instant) → L3, L4 (small code, independent) → L1 (incremental,
batchable) → L2 (fixtures) → L5, L6 (operator-assisted) → L8, L9 (after C1).
