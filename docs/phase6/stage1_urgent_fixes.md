# Stage 1 — Urgent fixes (runbook)

Source: 2026-06-10 audit (docs/audit/2026-06-10_pipeline_audit.md) -> grounding agents re-verified
every fact below against the live tree and DB on 2026-06-11, then an adversarial plan review
checked the runbooks end-to-end. **Hard deadline: the T-bill freshness gate first hard-fails a run
dated 2026-06-18** (lag>14 vs latest auction 2026-06-03; a 2026-06-17 run still passes).

## Execution order

0. **P-0 — Commit the working tree as the remediation baseline** (pulled forward from Stage-2 F-0):
   the whole phase-5 adapter buildout is uncommitted; every later diff assumes this baseline.
1. **P-1 — Backup before any purge** (pulled forward from Stage-2 F-1): Urgent-1/3 delete ~94MB of
   cache and 852 DB rows, and today there is NO offsite copy of 9.6GB of partially irreplaceable
   data. Minimum: `pg_dump -d mfs -Fc -f data/_archive/mfs_pre_stage1.dump` plus a tar (or restic
   init) of `data/raw/fbil_tbill/` and `data/raw/holdings/`. The full scheduled backup routine
   lands in Stage 2 (F-1); this is the one-shot safety copy.
2. **Urgent 1** (T-bill): code fix -> capture test fixtures -> purge -> **state reset** -> re-walk.
   Internal step order is load-bearing: fixtures are copied from files the purge deletes, and
   purging without resetting `rbi_state.json` would brick every run for ~115 days (see root cause).
3. **Urgent 2** (NAV hole): backfill immediately (operational), then durable fixes A (self-healing
   NAV stage) and B (interior-gap contract in Gate A). Step 4 modified — see the boxed note.
4. **Urgent 3** (quant wrong-month): code fixes BEFORE purges, so re-ingestion can't re-poison.
5. **Stage-1 close-out: one full `mfs pipeline` run.** Acceptance for the whole stage = a green run
   with Gate A passing honestly (no skip flags), a fresh computed_metrics partition at the new
   as_of, and a new shortlist dir. This supersedes the contaminated 2026-06-08 partition without
   deleting it (deletion happens in Stage-2 A1-10).

Two facts discovered during grounding that the audit missed (both already folded into the runbooks):
the poisoned-entry count is **801, not 793** (8 older WAF-hole prids), and prid 62892 — the start of
the poisoned block — is now a **real** press release blocked by its own poisoned cache entry, so the
missed auctions are unrecoverable until the purge.


## Urgent 1: RBI T-bill tip-discovery poisoned cache (pipeline halt expected 2026-06-18)

**Effort:** M

**Verified root cause** (re-checked against live tree/DB on 2026-06-11):

RBI now serves ~120KB 200-OK full-chrome shell pages for nonexistent prids. Verified against current code: (a) src/mfs/ingest/fbil_tbill.py:283-287 only rejects 200-responses with body<500 bytes, so shells pass `_fetch_prid_with_retry` and get cached at :311; (b) `_fetch_prid_html` (:298-300) and `_walk_prid_range._do` (:432-439) trust any cached file >200 bytes forever; (c) `discover_latest_prid` (:368-381) treats any returned html as "prid present", so `consecutive_absent` never reaches PROBE_FORWARD_GAP=12 and every run probes PROBE_FORWARD_MAX=400 new shells. Current cache (data/raw/fbil_tbill/rbi_press_releases/, 36,116 files): 801 shell files = 93.5 MB lacking `class="tableheader"` — the contiguous block prids 62892..63684 (793 files, audit-confirmed) PLUS 8 older WAF-hole prids (37471, 37472, 38330, 39259, 39552, 40997, 41620, 58640) the audit did not list. Audit verifier correction confirmed: shells DO match _DATE_RE (:124-127, IGNORECASE) via a lowercase 'date: Jun 08, 2026' chrome element; additionally verified that shells contain bare 'tableheader' in JavaScript, so detection MUST use the attribute form `class="tableheader"` (present in all 35,315 real pages incl. non-T-bill PRs, absent in all 801 shells), not a bare substring and not the Date marker. Live-probed RBI today (2026-06-11): prid 62892 is now a REAL PR (Jun 08, blocked by its poisoned cache entry), real PRs exist through ~62910 (Jun 10), live shells (122,407 bytes) start by 62915, and the RSS is stale at 62669 — so without the fix the missed auctions can never be re-fetched. Freshness gate (fbil_tbill.py:732-742; max_rf_lag_days=14 at config.py:55 / configs/pipeline.yaml:30) keys on latest AUCTION date = 2026-06-03 (tail of data/raw/fbil_tbill/rbi_scraped_91d_tbill.csv); DB risk_free_daily max(date)=2026-06-08 (4,957 rows) is just forward-fill to the last run date. lag>14 first triggers for a run dated 2026-06-18 (lag=15); 2026-06-17 still passes (lag=14). One extra hazard the audit missed: rbi_state.json has latest_prid=63684, so purging alone is NOT enough — the next walk's 50-prid tail re-check (start = 63684-50 = 63634, fbil_tbill.py:591) would hit 51/51 fresh shells = 100% failure rate > 5% cap (:619-628) and halt every run for ~115 days until RBI's real tip passes 63634; latest_prid must be reset to 62891 (verified highest real cached prid).

### Steps

**1. Detection fix: shell pages are transient fetch failures, never cached, never 'absent'**
  
*Files:* `src/mfs/ingest/fbil_tbill.py`

(a) After _TITLE_BLOCK_RE (line ~93) add: `_REAL_PR_MARKER_RE = re.compile(r'class="tableheader"', re.IGNORECASE)` and a helper `def _is_real_pr_page(html: str) -> bool: return _REAL_PR_MARKER_RE.search(html) is not None` (place after _looks_like_tbill_result, line ~123). MUST be the attribute form: shells contain bare 'tableheader' inside JS ($('.tableheader b') and a commented getElementsByClassName), so a bare-substring check false-positives; the 'Date :' marker also matches shells (lowercase 'date: Jun 08, 2026' chrome + IGNORECASE). (b) In _fetch_prid_with_retry, after the body<500 check at lines 283-287, add: `if not _is_real_pr_page(r.text): raise TransientHttpError(f"prid {prid}: 200 OK but WAF/shell page (no tableheader title block, body_len={len(r.text)})")`. Effect: tenacity retries 3x, then _fetch_prid_html returns (None, reason) WITHOUT writing the cache (atomic_write_text at :311 is only reached on success) — discover_latest_prid hits its existing `failure is not None -> probe abort` branch (:370-373) so a shell is never read as the tip nor as absence, and _walk_prid_range counts it toward the existing >5% failure-rate halt (:619-628). This preserves the fail-fast invariant: a WAF burst mid-walk halts the run instead of poisoning state.

**2. Cache-trust hardening: stop trusting >200-byte files; invalidate any cached shell on read**
  
*Files:* `src/mfs/ingest/fbil_tbill.py`

Add a helper `def _read_valid_cache(prid: int) -> str | None` that returns the cached text only if the file exists, size>200, reads OK, AND _is_real_pr_page(html); else None. Use it to replace the two trust sites: _fetch_prid_html lines 298-300 (`cache.exists() and cache.stat().st_size > 200` -> if _read_valid_cache returns None, fall through to the network fetch, which overwrites the bad file on success) and _walk_prid_range._do lines 432-439 (same: on invalid cache fall through to the fetch path). This makes the system self-healing if a shell ever lands in the cache again. Perf is a non-issue: both sites already read the full file; reparse_cache (:512-545) needs no change since _parse_tbill_html already returns None for shells (verified).

**3. Capture test fixtures BEFORE purging (prid_63000.html is deleted in step 5)**
  
*Files:* `tests/fixtures/rbi/`

Copy one real shell and one real auction PR from the live cache into fixtures. prid_63000.html: 120,666 chars, matches _DATE_RE ('date: Jun 08, 2026'), contains JS 'tableheader' but no class="tableheader" — preserves both false-positive traps. prid_62855.html: the real '91-Day, 182-Day and 364-Day T-Bill Auction Result: Cut-off' PR; verified _parse_tbill_html returns {auction_date: 2026-06-03, rate_annual_pct: 5.5586} (matches the scraped CSV tail).

```bash
mkdir -p /Users/suryavamseeayyagari/mfs/tests/fixtures/rbi && cp /Users/suryavamseeayyagari/mfs/data/raw/fbil_tbill/rbi_press_releases/prid_63000.html /Users/suryavamseeayyagari/mfs/tests/fixtures/rbi/shell_page.html && cp /Users/suryavamseeayyagari/mfs/data/raw/fbil_tbill/rbi_press_releases/prid_62855.html /Users/suryavamseeayyagari/mfs/tests/fixtures/rbi/cutoff_91d_2026-06-03.html
```

**4. Tests: shell -> transient failure (never cached, never absent); real -> parse; cached shell invalidated**
  
*Files:* `tests/test_fbil_tbill.py, tests/test_fbil_discovery.py`

In tests/test_fbil_tbill.py add: (1) test_shell_page_not_real_pr — load shell_page.html; assert fbil._DATE_RE.search(html) is not None AND 'tableheader' in html (documents the two traps) yet fbil._is_real_pr_page(html) is False and fbil._parse_tbill_html(html, 63000) is None. (2) test_real_cutoff_fixture_parses — cutoff fixture: _is_real_pr_page True; _parse_tbill_html == {'prid': 62855, 'auction_date': date(2026,6,3), 'rate_annual_pct': 5.5586}. (3) test_fetch_shell_raises_transient — call the undecorated parser path: monkeypatch fbil._rbi_client to an httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=shell_html))) and assert fbil._fetch_prid_with_retry.__wrapped__(client, 99999) raises TransientHttpError (use __wrapped__ to skip tenacity's 1-8s backoff). (4) test_shell_not_cached_and_counted_failed — monkeypatch fbil.paths.rbi_press_release_raw to tmp_path/'prid_{prid}.html' and fbil._fetch_prid_with_retry to raise TransientHttpError('shell'); assert fbil._fetch_prid_html(client, 5) == (None, 'shell') and no cache file exists; assert fbil._walk_prid_range(5, 5) == ([], 1). (5) test_cached_shell_invalidated — pre-write shell html to the tmp cache path for prid 5; monkeypatch _fetch_prid_with_retry to return the real cutoff html; _fetch_prid_html must IGNORE the cached shell, return the real html, and overwrite the cache file (assert 'class="tableheader"' now in file). In tests/test_fbil_discovery.py add test_shell_at_tip_aborts_probe using the existing _patch helper pattern (lines 34-40): fake _fetch_prid_html returns (None, 'shell page') for prid > real_max; assert discover_latest_prid returns real_max — i.e. shells neither advance the tip nor count as absence (same contract as existing test_transient_mid_probe_not_treated_as_tip at :58).

**5. Purge the 801 poisoned cache entries (93.5 MB), with a backup tarball first**
  
*Files:* `data/raw/fbil_tbill/rbi_press_releases/`

Shell pages are exactly the files lacking the literal `class="tableheader"` (verified: 801 of 36,116 files; 793 = the full contiguous prid range 62892..63684, plus 8 older holes 37471, 37472, 38330, 39259, 39552, 40997, 41620, 58640 which gap-fill will re-fetch as real PRs). Use find -exec (36k-file glob is near ARG_MAX). Run ONLY after step 1-2 are merged, or the next run re-poisons.

```bash
cd /Users/suryavamseeayyagari/mfs/data/raw/fbil_tbill/rbi_press_releases && find . -name 'prid_*.html' -exec grep -L 'class="tableheader"' {} + > /tmp/rbi_shell_pages.txt && wc -l < /tmp/rbi_shell_pages.txt && tar -czf /tmp/rbi_shell_pages_backup.tgz -T /tmp/rbi_shell_pages.txt && xargs rm < /tmp/rbi_shell_pages.txt
```

**6. Reset rbi_state.json latest_prid 63684 -> 62891 (REQUIRED — purge alone re-halts the pipeline)**
  
*Files:* `data/raw/fbil_tbill/rbi_state.json`

discover_latest_prid anchors at max(rss, state.latest_prid, cache_hwm) (fbil_tbill.py:354). RSS is live-verified stale (max prid 62669), and after the purge cache_hwm=62891, but state latest_prid=63684 would keep the anchor at 63684: the probe starts at 63685 (all shells -> abort, tip=63684) and the main walk's tail re-check starts at 63684-50=63634 (:591) — 51 fresh shell fetches = 100% failure rate -> IngestError every run until RBI's real tip passes 63634 (~115 days at 2600 prids/yr). Setting latest_prid=62891 (verified highest real cached prid) makes the probe walk 62892 upward, re-fetching the now-real PRs (live-verified real through ~62910) until the first live shell aborts it at the true tip. Do NOT use `mfs ingest tbill --backfill` — it deletes the whole state file (cli.py:102-106), losing oldest_prid_walked=27569. Keep oldest_prid_walked, last_walk_at, n_dates unchanged.

```bash
python3 -c "import json,pathlib; p=pathlib.Path('/Users/suryavamseeayyagari/mfs/data/raw/fbil_tbill/rbi_state.json'); s=json.loads(p.read_text()); assert s['latest_prid']==63684, s; s['latest_prid']=62891; p.write_text(json.dumps(s, indent=2, sort_keys=True)); print(s)"
```

**7. Re-walk (operator step, network): incremental tbill ingest, ~3-7 min**
  
*Files:* `src/mfs/ingest/fbil_tbill.py via CLI`

No cold re-walk is needed — 35,315 real cached pages and oldest_prid_walked=27569 are preserved. The run does: reparse_cache over 35,315 files (~30s, audit-measured 28.7s), probe-forward 62892->true tip (~20-30 real fetches at ~1/s, aborting at the first live shell), gap-fill re-fetch of the 8 old holes plus cache re-read (~1-2 min), main walk 62841..tip (mostly probe-cached). Expect total ~3-7 min, recovering the Jun 08-10 PRs including the 2026-06-10 weekly auction (auctions are Wednesdays; Jun 10 real PRs live-verified at prids 62900-62910). This resets the freshness deadline from 2026-06-18 to ~2026-06-25 and each subsequent weekly auction extends it.

```bash
cd /Users/suryavamseeayyagari/mfs && uv run mfs ingest tbill
```

### Acceptance checks

- Unit tests pass: `cd /Users/suryavamseeayyagari/mfs && uv run pytest tests/test_fbil_tbill.py tests/test_fbil_discovery.py -q` -> all pass, including the 6 new tests (shell fixture rejected + not cached + counted as failure + probe abort; real fixture parses to 2026-06-03 / 5.5586; cached shell invalidated and overwritten).
- Purge complete: `cd /Users/suryavamseeayyagari/mfs/data/raw/fbil_tbill/rbi_press_releases && find . -name 'prid_*.html' -exec grep -L 'class="tableheader"' {} + | wc -l` -> 0, and `ls prid_*.html | wc -l` -> 35315 (was 36116; 801 removed).
- Cache high-water mark is the last real PR: `ls /Users/suryavamseeayyagari/mfs/data/raw/fbil_tbill/rbi_press_releases | sed 's/prid_//;s/\.html//' | sort -n | tail -1` -> 62891.
- State reset: `cat /Users/suryavamseeayyagari/mfs/data/raw/fbil_tbill/rbi_state.json` -> latest_prid: 62891, oldest_prid_walked: 27569 (unchanged).
- After step 7 (operator): `tail -3 /Users/suryavamseeayyagari/mfs/data/raw/fbil_tbill/rbi_scraped_91d_tbill.csv` -> includes an auction date of 2026-06-10 (and 2026-06-03, 5.5586 still present); the run exits 0 with no IngestError and the log line fbil.rbi.discover_latest_prid shows tip in ~62905-62915, probed << 400.
- After step 7: `psql -d mfs -c "SELECT max(date) FROM risk_free_daily;"` -> the run date (>= 2026-06-11; was 2026-06-08), and freshness lag = run_date - 2026-06-10 <= 14, so Gate A passes through ~2026-06-25 and rolls forward weekly.
- No re-poisoning: immediately after the step-7 run, re-run the grep -L count from check 2 -> still 0 (shells encountered at the live tip were aborted on, not cached).
- Regression guard on existing behavior: `uv run pytest tests/ -q -k 'fbil or tbill'` -> existing parser/discovery tests (old per-tenor titles, 182/364 rejection, stale-RSS tip recovery, transient-mid-probe) still pass.

### Blast radius & rollback

Code changes are confined to src/mfs/ingest/fbil_tbill.py (detection + cache-trust) plus tests/fixtures; paths.py:45-46 is the only other reference to the cache dir and nothing else consumes it. No DB schema or writer changes — risk_free_daily is upsert-only and no poisoned rates ever reached it (shells parse to None; verified). The scraped CSV and manual-CSV override path are untouched: data/raw/fbil_tbill/manual/ does not exist (verified), and rbi_scraped_91d_tbill.csv is rebuilt from reparse_cache() each run (fbil_tbill.py:573), so purging shells loses zero real dates. Interaction with the next pipeline run: ORDER MATTERS — the code fix must be deployed before the purge/next run, else the very next probe re-caches up to 400 fresh shells; conversely the purge without the state reset (step 6) converts the halt-by-staleness into a halt-by-failure-rate (51/51 shell tail walk) lasting ~115 days. Once all three land, the next run self-heals (recovers Jun 08-10 PRs, including the missed 2026-06-10 auction), warm tbill stage time drops ~7 min/run, and the ~48MB/run cache growth stops. Behavior change to accept: probe absence detection (12 consecutive 404s) is now effectively dead since RBI serves 200-OK shells; tip discovery ends at the first shell past the tip via the existing probe-abort branch — and a genuine WAF burst against real prids mid-walk now halts the run via the >5% failure cap, which is the intended fail-fast invariant (no partial risk-free data). Hard deadline: without this fix, every pipeline run dated >= 2026-06-18 hard-fails Gate A (lag 15 > 14 from the 2026-06-03 auction) and cannot self-heal; today is 06-11, so there are 7 days of slack. Rollback: code is a git revert; the 801 purged files are tarred to /tmp/rbi_shell_pages_backup.tgz before deletion (and are re-fetchable — RBI prids are permanent); rbi_state.json is regenerated by every successful walk, so the edit is self-correcting.


## Urgent 2: nav_daily 8-trading-day hole (2026-05-23..06-04) + no interior-gap detection

**Effort:** M

**Verified root cause** (re-checked against live tree/DB on 2026-06-11):

The pipeline's only NAV stage is a snapshot: /Users/suryavamseeayyagari/mfs/src/mfs/cli.py:1107 runs `_stage("ingest navs (today)", amfi_nav.ingest_today)`. ingest_today (src/mfs/ingest/amfi_nav.py:196-219) fetches NAVAll.txt, which carries ONE row per scheme (its latest NAV) — days missed between runs are never fetched. `ingest_since` (amfi_nav.py:293-295, bulk-history endpoint, 90-day windows per configs/pipeline.yaml ingest.amfi_nav.bulk_window_days: 90) exists but no pipeline path calls it. No gate sees interior gaps: freshness.py:92-97 checks only business-day lag of global MAX(nav_date); coverage.py's nav_daily Gate A contract (coverage.py:103-109, entity check at 341-348) checks per-scheme MAX(nav_date) vs a stale cutoff; orchestrator._data_quality_flag (src/mfs/compute/orchestrator.py:37-57) tolerates 5% missing over 3y (~37 days). VERIFIED CURRENT STATE (2026-06-11, worse than the audit): nav_daily MAX = 2026-06-08; vs the NIFTY 50 TRI calendar, rows/day: 05-22=7,875 (itself ~700 short of the ~8,600 norm — audit's 05-23 start is slightly optimistic, backfill must include 05-22), 05-25/05-26/05-27/06-02/06-03 = 0, 05-29=12, 06-01=5, 06-04=20, 06-05=7,913, 06-08=823 (snapshot taken before that evening's NAVAll), and 06-09/06-10 not ingested at all. Freshness Gate A still PASSES today (lag 06-08→06-11 = 3 bdays ≤ max_nav_lag_bdays: 5). computed_metrics partitions 2026-06-08 (665 rows) and 2026-05-25 (682 rows) were computed over the hole. Calibration for the new gate: over the trailing 3y (743 NIFTY-50-TRI trading days) exactly 18 days have <4,000 nav rows — 9 are legitimate special sessions (Muhurat/budget Saturdays, 619-1,074 rows) and 9 are the current hole. One audit correction: only raw cache file 2026-04-23_2026-05-22.txt overlaps the hole, so `--since 2026-05-22` (window file 2026-05-22_2026-06-11.txt) gets a fresh fetch — but the skip-on-presence cache at amfi_nav.py:247-249 IS a same-day-retry hazard for the durable fix.


> **MODIFIED BY PLAN REVIEW — step 4 is now ARCHIVE, not delete.** Several Stage-2 acceptance
> checks were anchored to the 2026-06-08 partition. Resolution: archive it now, delete it later
> inside Stage-2 task A1-10 (the coordinated recompute). Replace step 4's command with:
>
> ```bash
> mkdir -p data/_archive
> psql -d mfs -c "\copy (SELECT * FROM computed_metrics WHERE as_of_date='2026-06-08') TO 'data/_archive/computed_metrics_2026-06-08.csv' WITH (FORMAT csv, HEADER)"
> tar -czf data/_archive/shortlist_2026-06-08.tgz -C data/output/shortlist 2026-06-08
> ```
>
> The partition stays in place (clearly superseded once the Stage-1 closing pipeline run writes a
> fresh, post-backfill partition). All Stage-2 acceptance commands that said `--as-of 2026-06-08`
> are re-anchored to the latest post-A1-10 partition (see Stage-2 doc, Cross-workstream contract).

### Steps

**1. Backfill the hole now (operational)**
  
*Files:* `/Users/suryavamseeayyagari/mfs/src/mfs/ingest/amfi_nav.py:222-295, /Users/suryavamseeayyagari/mfs/src/mfs/db/writers.py:77-93`

Run the incremental NAV backfill from the last fully-populated day, inclusive. Routing: cli.py:29-50 `mfs ingest navs --since` -> amfi_nav.ingest_since(2026-05-22) -> ingest_backfill(start=2026-05-22, end=today) = a single bulk window (20 days < bulk_window_days 90), saved as data/raw/amfi_nav_history/2026-05-22_<today>.txt (no cache collision; only 2026-04-23_2026-05-22.txt exists). fail_on_missing defaults True, so an empty/failed window raises IngestError (fail-fast preserved). Upsert is PK-clean over the sparse days: writers.upsert_nav_daily (db/writers.py:77-93) does ON CONFLICT (scheme_code, nav_date) DO UPDATE via _copy_upsert, so the 12-20 stray rows on 05-29/06-01/06-04 and the partial 05-22/06-05/06-08 days are updated in place and missing rows inserted; it then runs refresh_fund_log_returns for all ~8.6k affected schemes (one pass, ~3 min — known perf item, acceptable here). Also run `uv run mfs ingest benchmarks` so benchmark_daily (also MAX=2026-06-08) covers 06-09..today and the verification calendar is complete. Expect roughly 8.6k rows x ~13 trading days plus weekend overnight-fund rows (~115-175k rows).

```bash
uv run mfs ingest navs --since 2026-05-22 && uv run mfs ingest benchmarks
```

**2. Durable fix A: self-healing pipeline NAV stage**
  
*Files:* `/Users/suryavamseeayyagari/mfs/src/mfs/ingest/amfi_nav.py:196-300 (new fn + line 247), /Users/suryavamseeayyagari/mfs/src/mfs/cli.py:1107`

Add `ingest_incremental()` to src/mfs/ingest/amfi_nav.py: read MAX(nav_date) (q.latest_dates()['nav_latest'], db/queries.py:395-398); if None raise IngestError('nav_daily empty - run mfs ingest navs --backfill'); else `n1, y1 = ingest_since(latest)` — note ingest_since's start is INCLUSIVE, so a partial last day (e.g. 06-08's 823 rows) is re-fetched and repaired — then `n2, y2 = ingest_today()` (the bulk endpoint may not yet include today's NAVs at run time; NAVAll provides each scheme's freshest row); return (n1+n2, sorted(set(y1)|set(y2))). Swap the call site cli.py:1107 from `_stage("ingest navs (today)", amfi_nav.ingest_today)` to `_stage("ingest navs (incremental)", amfi_nav.ingest_incremental)`. Kill the skip-on-presence hazard for the live window: at amfi_nav.py:247 change `if raw_path.exists():` to `if raw_path.exists() and to_d < date.today():` so a window ending today is always re-fetched (historical windows stay cached/immutable; a same-day retry after a sparse AMFI response no longer replays stale bytes). Invariants kept: incremental-by-default with `mfs ingest navs --backfill` as the --full-style escape hatch; ingest_backfill still raises on failed windows.

**3. Durable fix B: interior-gap contract in Gate A (BLOCKING)**
  
*Files:* `/Users/suryavamseeayyagari/mfs/src/mfs/coverage.py:62-66,84-167,286-300,334-363; /Users/suryavamseeayyagari/mfs/src/mfs/config.py:~54-69; /Users/suryavamseeayyagari/mfs/configs/pipeline.yaml freshness block`

Belongs in coverage.py, NOT freshness.py: freshness is end-recency-only by design, Gate B never halts, and Gate A runs at cli.py:1130 immediately after the NAV stage — so fix A heals, Gate A proves the heal (making run-pipeline.md:32's 'coverage gate proves the gap closed' claim true). Changes: (a) add field `interior_gap: bool = False` to Contract (coverage.py:84-98) and set `interior_gap=True` on the nav_daily contract (coverage.py:103-109); (b) new DB helper next to _bounds (coverage.py:286): `_nav_interior_gap_days(end: date, n_days: int = 30, floor_frac: float = 0.5) -> list[date]` — SQL: take the last n_days dates from `SELECT DISTINCT date FROM benchmark_daily WHERE ticker='NIFTY 50 TRI' AND date <= %(end)s ORDER BY date DESC LIMIT 30` (end = have_till, so end-staleness stays the STALE check's job), LEFT JOIN per-day `COUNT(*)` from nav_daily, and return dates where count < floor_frac * (median daily count over the trailing 3y). Calibration verified live: median ~8,600, so floor ~4,300; special sessions (619-1,074 rows, <=1 per any 30-trading-day span: 2023-11-12, 2024-01-20, 2024-03-02, 2024-05-18, 2024-11-01, 2025-02-01, 2025-10-21, 2026-02-01) trip the floor, the current hole's 9 days trip it harder; (c) in evaluate() (coverage.py:334-363), when contract.interior_gap and status would be OK/GAP, compute gap_days; if len(gap_days) > cfg.max_nav_interior_gap_days set status to new constant INTERIOR_GAP with ok=False for BLOCKING, detail listing the missing dates and remediation 'mfs ingest navs --since <first missing date>'; (d) config: add `max_nav_interior_gap_days: int | None = 2` to FreshnessConfig (src/mfs/config.py, class at ~line 54; null disables, mirroring the other freshness keys — this is the rollback switch) and `max_nav_interior_gap_days: 2` to the freshness block of configs/pipeline.yaml. Threshold rationale to record in the yaml comment: quality.max_missing_pct_in_window 0.05 (config.py:47) is the PER-SCHEME 3y tolerance (~37 days) and can never catch a fresh systemic hole (current hole = 1.2% of 3y); the gate instead allows 2 sub-floor days per 30 trading days (~6.7%), one above the special-session base rate of <=1 per 30. Optionally also flag (advisory detail, non-blocking) when trailing-3y sub-floor days exceed 0.05*calendar as a catastrophic-accumulation backstop (today: 18/743 = 2.4%, passes).

**4. Disposition of the tainted computed_metrics partitions**
  
*Files:* `/Users/suryavamseeayyagari/mfs/src/mfs/rank/shortlist.py:131, /Users/suryavamseeayyagari/mfs/src/mfs/db/queries.py:357-362, /Users/suryavamseeayyagari/mfs/src/mfs/db/writers.py:175-216`

No invalidation is REQUIRED for default readers: shortlist.build_scored_stage1 (src/mfs/rank/shortlist.py:131) defaults as_of to q.latest_computed_metrics_date() = MAX(as_of_date) (db/queries.py:357-362), and `mfs pipeline` passes an explicit as_of=today — so the next pipeline run writes a fresh partition that supersedes 2026-06-08 everywhere by default. The 06-08 partition is only read if a date is passed explicitly. Recommended hygiene: purge the hole-contaminated partition and its on-disk outputs so nobody consumes them later (it is fully re-creatable: upsert_computed_metrics_phase1 at db/writers.py:197-216 DELETEs the as_of partition then re-inserts, so `uv run mfs compute phase1 --as-of 2026-06-08` after backfill rebuilds it point-in-time-correct if a 06-08 partition is ever wanted; note that DELETE also clears phase-2 columns, so a bare phase1 rebuild leaves stage-2 metrics null for that date). The 2026-05-25 partition (682 rows) saw only the first ~1-2 missing days — note it in the run log, purge optional.

```bash
psql -d mfs -c "DELETE FROM computed_metrics WHERE as_of_date = '2026-06-08';" && rm -rf /Users/suryavamseeayyagari/mfs/data/output/shortlist/2026-06-08
```

**5. Tests**
  
*Files:* `/Users/suryavamseeayyagari/mfs/tests/test_coverage.py, /Users/suryavamseeayyagari/mfs/tests/test_incremental_ingest.py, /Users/suryavamseeayyagari/mfs/tests/test_amfi_history_layout.py`

(a) tests/test_coverage.py — reuse the _patch_db pattern (test_coverage.py:136): monkeypatch the new `cov._nav_interior_gap_days` to return [3 fake dates] with max_nav_interior_gap_days=2 -> evaluate(nav contract) yields status INTERIOR_GAP, ok=False; with 2 dates -> OK/ok=True; with cfg value None -> check skipped entirely; non-nav contracts (interior_gap=False) never call the helper. (b) tests/test_incremental_ingest.py — add ingest_incremental tests mirroring the existing benchmark-watermark tests (test_incremental_ingest.py:48): monkeypatch q.latest_dates to return nav_latest=date(2026,6,8), monkeypatch amfi_nav.ingest_since and ingest_today to record calls -> assert ingest_since called with date(2026,6,8) (inclusive watermark) and ingest_today called after; nav_latest=None -> pytest.raises(IngestError). (c) cache-bypass test in tests/test_amfi_history_layout.py or test_incremental_ingest.py: pre-create the raw window file for a window ending today with stale bytes, monkeypatch fetch_window to return fresh bytes + a no-op writers.upsert_nav_daily -> assert fetch_window WAS called (cache ignored because to_d == today) and that a window ending yesterday still uses the cache.

```bash
uv run pytest tests/test_coverage.py tests/test_incremental_ingest.py tests/test_amfi_history_layout.py -q
```

**6. Doc sync**
  
*Files:* `/Users/suryavamseeayyagari/mfs/.claude/commands/run-pipeline.md:32,69,156`

Update .claude/commands/run-pipeline.md line 69 ('ingest navs — AMFI daily NAVAll. Incremental (~5 s)...') to describe the self-healing stage: 'fetches MAX(nav_date)->today via the bulk endpoint, then today\'s NAVAll snapshot', and add Gate A's INTERIOR_GAP status + remediation to the Gate A bullet (line 156). Line 32's 'the coverage gate then verifies the gap closed' becomes accurate for NAV only after step 3 lands — keep the wording, it now holds.

### Acceptance checks

- Hole closed (run after step 1): psql -d mfs -c "WITH cal AS (SELECT DISTINCT date AS d FROM benchmark_daily WHERE ticker='NIFTY 50 TRI' AND date >= DATE '2026-05-22'), daily AS (SELECT nav_date, COUNT(*) cnt FROM nav_daily WHERE nav_date >= DATE '2026-05-22' GROUP BY nav_date) SELECT cal.d, COALESCE(daily.cnt,0) FROM cal LEFT JOIN daily ON daily.nav_date=cal.d WHERE COALESCE(daily.cnt,0) < 4000 ORDER BY cal.d;" -> expected: 0 rows (before the fix this returns 10 rows: 05-25,26,27=0; 05-29=12; 06-01=5; 06-02,03=0; 06-04=20; 06-08=823; plus 06-09/06-10 once benchmarks refresh).
- Log-return cache consistent with NAV: psql -d mfs -c "SELECT (SELECT MAX(nav_date) FROM nav_daily) AS nav_max, (SELECT MAX(date) FROM fund_log_returns) AS flr_max;" -> expected: both equal (today or last trading day).
- Backfill idempotency: re-run `uv run mfs ingest navs --since 2026-05-22` -> exits 0, second run upserts the same rows (no duplicate-key errors; nav_daily count unchanged or +0).
- Gate A detects a hole (unit): uv run pytest tests/test_coverage.py -q -> all pass, including new INTERIOR_GAP tests (3 missing days > threshold 2 -> blocking failure; <=2 -> OK; threshold null -> skipped).
- Self-healing stage wiring (unit): uv run pytest tests/test_incremental_ingest.py tests/test_amfi_history_layout.py -q -> all pass, including ingest_since(MAX(nav_date)) inclusive-watermark, IngestError-on-empty-DB, and cache-bypass-for-current-window tests.
- Gate A live check after steps 1-3: uv run python -c "from mfs import coverage; r = coverage.run_gate('A', raise_on_block=False); print(coverage.render(r))" -> nav_daily row shows [OK]; then temporarily set max_nav_interior_gap_days: 0 in configs/pipeline.yaml and re-run -> nav_daily shows [INTERIOR_GAP] listing the special-session dates and GATE A FAILED (revert the yaml after).
- Partition disposition (after step 4 purge): psql -d mfs -c "SELECT as_of_date, COUNT(*) FROM computed_metrics GROUP BY as_of_date ORDER BY as_of_date DESC LIMIT 3;" -> 2026-06-08 absent; next full `mfs pipeline` run writes a fresh partition at its as_of and shortlist defaults to it (shortlist.py:131 MAX(as_of_date)).

### Blast radius & rollback

Touches: amfi_nav.py (new ingest_incremental + cache-bypass condition), cli.py:1107 (one call-site), coverage.py (new Contract field, status, helper — the contract-registry completeness test in test_coverage.py must still pass), config.py FreshnessConfig + configs/pipeline.yaml (new key; pydantic default 2 keeps old yamls valid), run-pipeline.md. Each NAV upsert already triggers refresh_fund_log_returns for affected schemes (~3 min per call, writers.py:91-92); fix A makes two ingest calls per pipeline run (bulk window + NAVAll), so the NAV stage roughly doubles to ~6 min on a warm day — acceptable, and the deeper churn issue is a separate audit item (writers.py refresh-per-window). Interaction with the next pipeline run: NAV stage now heals any gap since the last run automatically; Gate A's INTERIOR_GAP check halts the run (exit 2) if AMFI's bulk endpoint cannot supply the missing days — that is the desired fail-fast behavior, not a regression; a genuinely unhealable AMFI outage now blocks ranking instead of silently ranking over a hole. The 06-08 partition purge only affects explicit as_of=2026-06-08 reads (default readers use MAX(as_of_date)); it is re-creatable via `uv run mfs compute phase1 --as-of 2026-06-08`. Rollback: revert cli.py:1107 to ingest_today (one line); set max_nav_interior_gap_days: null to disable the gate check without code changes; the backfill itself is a pure idempotent upsert with no rollback needed. No invariant violations: no manual data entry, AUM untouched, incremental-by-default preserved with --backfill as the escape hatch.


## Urgent 3: quant April portfolios stored as May 2026; month-keyed cache permanently poisoned

**Effort:** M

**Verified root cause** (re-checked against live tree/DB on 2026-06-11):

quant's WebForms discovery (src/mfs/ingest/holdings/quant.py:100-138, `discover_scheme_urls`) trusts the requested month id sent to displaydisclouser2; for ym=2026-05 the endpoint served the April files. Nothing validates the artifact's internal statement date: holdings/_run.py:67 stamps as_of_month purely from ym, and the 'AS ON' banner appears only in docstrings (grep confirms no code parses it). The poison is permanent because GenericHoldingsAdapter.fetch_excel (src/mfs/ingest/holdings/_generic.py:322-326) returns any existing cached file (`if out.exists(): return out`), so a genuine May file can never replace data/raw/holdings/quant/2026-05/. VERIFIED LIVE (2026-06-11): all 29 files in data/raw/holdings/quant/2026-05/ are md5-identical to data/raw/holdings/quant/2026-04/ (per-file md5 loop: 29/29 IDENTICAL); the cached May file's sheet-0 row index 3 reads 'MONTHLY PORTFOLIO STATEMENT AS ON 30 Apr 2026'; holdings_monthly has quant at both 2026-04-01 and 2026-05-01 with 29 schemes / 852 rows each and a symmetric diff of 0 rows on (scheme_code, isin, weight_pct). All audit file:line citations check out against the current tree (quant.py:100, _generic.py:322-326 [audit said 324], _run.py:67). One audit nuance: the parse-skip marker cache (_parse_cache.py) is used ONLY by the managers/factsheet path (managers/_run.py:24,125-140,186), not holdings — the holdings poisoning is purely the fetch_excel skip-if-exists, so the purge needs no marker cleanup.

### Steps

**1. Durable fix (land BEFORE any purge or pipeline run): add statement-date validation to the generic SEBI parser**
  
*Files:* `src/mfs/errors.py, src/mfs/ingest/holdings/_generic.py, src/mfs/ingest/holdings/quant.py`

(a) errors.py: add `class StatementDateMismatchError(IngestError)` carrying attrs `artifact_path: Path`, `expected_ym: str`, `found_yms: set[str]`. (b) _generic.py: add helper `find_statement_months(rows: list[tuple]) -> set[str]` that scans only the PRE-HEADER rows (rows[:header_idx] from the existing `_detect_header`, _generic.py:123) for an 'as on' phrase (case-insensitive) followed by a parseable month+year, returning each as 'YYYY-MM'. Must handle the formats verified in the live cache: '30 Apr 2026' (quant, edelweiss), 'April 30, 2026' and 'April 30,2026' (most AMCs), '30-Apr-2026'/'30-APR-2026' (hdfc, groww, the_wealth_company), '30th April 2026' (baroda), '29 May 2026*' (hsbc), and the split-cell variant where the cell is just 'AS ON :' and the date (string OR datetime cell value) sits in a later cell of the same row (capitalmind, helios, sbi, taurus). 'as on' with NO parseable date (e.g. tata's 'NAV As on Record Date') yields nothing. (c) Thread a new kwarg `expect_ym: str | None = None` plus `require_statement_date: bool = False` through `parse_sebi_excel` (_generic.py:227) into `_parse_one_sheet` (_generic.py:151): after header detection, compute found = find_statement_months(rows[:header_idx]); if expect_ym and found and expect_ym not in found -> raise StatementDateMismatchError; if expect_ym and not found and require_statement_date -> raise. The any-match-passes rule tolerates a lagging riskometer 'as on' date when the true portfolio banner also matches. (d) GenericHoldingsAdapter.parse_excel (_generic.py:328-334) passes `expect_ym=ym, require_statement_date=self.require_statement_date` (new class attr, default False) — this auto-covers the ~23 adapters that inherit parse_excel. (e) quant.py: set `require_statement_date = True` on QuantHoldingsAdapter (banner verified always present at sheet-0 row 3).

**2. Make a mismatch abort the AMC and NEVER leave the artifact cached**
  
*Files:* `src/mfs/ingest/holdings/_run.py`

In run_for_amc's per-scheme loop, the parse call at _run.py:120 currently catches ALL exceptions and continues (lines 121-127). Add a dedicated `except StatementDateMismatchError as e:` BEFORE the blanket except: (1) `excel_path.unlink(missing_ok=True)` so the wrong-month file is evicted from data/raw/holdings/<amc>/<ym>/ (this is the 'DO NOT cache' guarantee — download happens at _run.py:111 before parse, so eviction must be explicit), (2) log `holdings.month_mismatch` with amc/scheme/expected/found, (3) re-raise. Because the raise escapes run_for_amc before the DB transaction at _run.py:172-182, NO DELETE+reinsert occurs — existing months stay intact and the AMC is skipped entirely this run (fail-fast invariant). run_all's per-AMC isolation (_run.py:221-232) records it as that AMC's failure; other AMCs proceed; pipeline does not crash. Note: download+parse are interleaved per scheme, so the first mismatch aborts before further downloads — at most one wrong file is fetched and it is unlinked.

**3. Mechanical pass: thread expect_ym through adapters that call parse_sebi_excel directly**
  
*Files:* `src/mfs/ingest/holdings/{bajaj_finserv,axis,groww,dsp,iti,quantum,sundaram,trust}.py`

These 8 adapters override parse_excel but delegate to parse_sebi_excel (grep 'parse_sebi_excel(' confirms the call sites); each override already receives `ym`, so add `expect_ym=ym` to each call (one line per file). The fully bespoke parsers (hdfc, sbi, nippon, franklin, abakkus, absl, shriram, icici_pru, etc.) do not call parse_sebi_excel — leave them for the Stage-2 generic sweep; validate-when-present semantics means they are simply unchanged, not broken.

**4. Pre-purge evidence snapshot + false-positive sweep over the live cache**
  
*Files:* `data/raw/holdings/`

Run AFTER steps 1-3 are merged and BEFORE the purge. Expected: exactly 29 MISMATCH lines, all under data/raw/holdings/quant/2026-05/ reporting found=['2026-04']; zero mismatches elsewhere (this empirically bounds generic-check false positives at 0 across all 47 AMCs' cached artifacts). If any non-quant mismatch appears, inspect that file before enabling — it is either another live wrong-month instance (good catch) or a regex gap to fix in find_statement_months.

```bash
cd /Users/suryavamseeayyagari/mfs && uv run python -c "
import glob, openpyxl
from mfs.ingest.holdings._generic import find_statement_months
import re
bad = ok = none = 0
for f in glob.glob('data/raw/holdings/*/20*/*.xlsx'):
    ym = f.split('/')[3]
    try:
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        rows = list(wb.worksheets[0].iter_rows(values_only=True))[:12]; wb.close()
    except Exception: continue
    found = find_statement_months(rows)
    if not found: none += 1
    elif ym in found: ok += 1
    else: bad += 1; print('MISMATCH', f, sorted(found))
print('ok', ok, 'none', none, 'mismatch', bad)"
```

**5. Purge the poisoned cache directory (29 files, verified no markers/tmp present)**
  
*Files:* `data/raw/holdings/quant/2026-05/`

Removes the 29 byte-identical-to-April xlsx files so the next genuine May publication can be downloaded (fetch_excel's exists() check is per-file). Leave data/raw/holdings/quant/2026-04/ untouched — it is the correct April data (its 30th file, 'quant_Flexi_Cap_Fund_Apr_2026.xlsx', is a harmless URL-named leftover from an early run; current filenames are '<printed name>.xlsx' so it is never read).

```bash
rm -rf /Users/suryavamseeayyagari/mfs/data/raw/holdings/quant/2026-05
```

**6. Purge the mis-stamped DB partition — REQUIRES USER APPROVAL (write to live DB)**
  
*Files:* `holdings_monthly table (Postgres db 'mfs')`

Expected output: DELETE 852 (verified current row count for that partition). There is no standalone app-level purge path — the app's only DELETE for a (source_amc, as_of_month) partition runs inside a successful re-ingest (_run.py:177-181), which cannot happen until quant actually publishes May (and after step 1, a re-run against the still-April endpoint correctly aborts instead of writing). So an approved psql DELETE is the right mechanism. DECISION (confirmed against code): after the purge, 2026-05 stays ABSENT for quant until quant publishes real May files — fail-fast prefers absence over wrongness. Gate B treats this as an advisory gap, not a halt: the holdings contract is ADVISORY (coverage.py:131-135) and _classify returns ok=True for advisory regardless of status (coverage.py:276-277); max_holdings_lag_days is null (configs/pipeline.yaml:36). If the parallel remediation item enables a 75-day holdings threshold, quant's 2026-04-30 data is 42 days old today — still inside it, no conflict.

```bash
psql -d mfs -c "DELETE FROM holdings_monthly WHERE source_amc = 'quant' AND as_of_month = DATE '2026-05-01';"
```

**7. Tests: April-banner-as-May fixture is rejected and not cached**
  
*Files:* `tests/test_holdings_generic.py (extend), tests/test_holdings_month_validation.py (new)`

(a) test_holdings_generic.py — reuse the existing _write(tmp_path, rows) synthetic-workbook helper: (i) workbook with banner 'MONTHLY PORTFOLIO STATEMENT AS ON 30 Apr 2026' above a valid SEBI table: parse_sebi_excel(p, 'X', 'quant', expect_ym='2026-05') raises StatementDateMismatchError, and with expect_ym='2026-04' parses normally; (ii) find_statement_months format matrix: 'April 30, 2026', 'April 30,2026', '30-Apr-2026', '30th April 2026', '29 May 2026*', split-cell ['AS ON :', datetime(2026,4,30)], and 'NAV As on Record Date' -> empty set; (iii) banner absent: parses when require_statement_date=False, raises when True. (b) tests/test_holdings_month_validation.py — runner-level (pattern: tests/test_adapter_isolation.py): register a stub GenericHoldingsAdapter whose discover returns one URL and whose fetch_excel copies an April-banner fixture to the paths.holdings_excel_raw location for ym='2026-05'; monkeypatch mfs.db.queries.scheme_master and mfs.db.writers.upsert_holdings (writer must pytest.fail if called); assert run_for_amc(stub, ym='2026-05') raises StatementDateMismatchError AND the cached file no longer exists (unlink happened) AND the writer was never called.

**8. (Optional, network + USER approval, only after quant publishes May) re-ingest May**

Before quant publishes: this now FAILS with StatementDateMismatchError and leaves no cache file and no DB rows (correct fail-fast). After quant publishes: expect rows_written≈852, schemes≈29, and a fresh data/raw/holdings/quant/2026-05/ whose banners read 'AS ON 31 May 2026'. The regular `mfs pipeline` June run does the same via holdings.run_all() (cli.py:1158).

```bash
cd /Users/suryavamseeayyagari/mfs && uv run mfs ingest holdings --amc quant --ym 2026-05
```

**9. Stage-2 note (do not fix now): same wrong-month class in the managers/factsheet path**
  
*Files:* `src/mfs/ingest/managers/_run.py:96-100`

adapter.fetch(ym) caches the factsheet PDF month-keyed and as_of_month is stamped from ym (managers/_run.py:96, 218, 256) with no internal-date validation of the PDF; a 'latest factsheet' URL serving the prior month would poison PTR/factsheet-holdings the same way, and the _parse_cache marker (managers/_run.py:125-140) would then SKIP re-parsing the byte-identical wrong PDF on every incremental run, deepening the poison. Log as a Stage-2 task: validate the factsheet's printed data month (cover page) against ym, validate-when-present, and on mismatch do not write a marker and delete the cached PDF.

### Acceptance checks

- Unit tests pass: cd /Users/suryavamseeayyagari/mfs && uv run pytest tests/test_holdings_generic.py tests/test_holdings_month_validation.py -q -> all pass, including the April-as-May rejection and not-cached/not-written assertions.
- Pre-purge sweep (step 4 command) -> prints exactly 29 'MISMATCH data/raw/holdings/quant/2026-05/... ['2026-04']' lines and 'mismatch 29' with zero mismatches outside quant/2026-05 (proves the generic check has no false positives on the entire live cache).
- Cache purged: ls /Users/suryavamseeayyagari/mfs/data/raw/holdings/quant -> only '2026-04' (30 files retained).
- DB purged: psql -d mfs -c "SELECT count(*) FROM holdings_monthly WHERE source_amc='quant' AND as_of_month='2026-05-01';" -> 0; and psql -d mfs -c "SELECT max(as_of_month), count(DISTINCT scheme_code) FROM holdings_monthly WHERE source_amc='quant';" -> 2026-04-01 | 29.
- April partition untouched: psql -d mfs -c "SELECT count(*) FROM holdings_monthly WHERE source_amc='quant' AND as_of_month='2026-04-01';" -> 852.
- Gate B treats the absent May as advisory gap, not halt: cd /Users/suryavamseeayyagari/mfs && uv run mfs coverage --gate B; echo exit=$? -> holdings contract line shows the lag/gap, exit=0 (no blocking failure; coverage.py:276-277 advisory ok=True).
- No DB-side duplicate-month signature remains: psql -d mfs -c "SELECT source_amc FROM (SELECT source_amc, as_of_month, md5(string_agg(scheme_code||isin||weight_pct::text, ',' ORDER BY scheme_code, isin)) h FROM holdings_monthly GROUP BY 1,2) t GROUP BY source_amc, h HAVING count(*)>1;" -> 0 rows (quant was the only AMC with identical consecutive months).

### Blast radius & rollback

DB purge: removes 852 rows that are byte-identical (verified symmetric diff = 0) to the retained quant 2026-04-01 partition, so every consumer that takes per-scheme LATEST holdings (aum_impact via orchestrator.py:244 unbounded max as_of_month, stage-3 overlap) computes identical VALUES afterwards — only the month stamp reverts to 2026-04-01. index_constituents_monthly contains only 2026-04-01 (verified), so derived constituents are unaffected; active_share is null universe-wide regardless. Code change: validation goes live for the ~23 adapters inheriting GenericHoldingsAdapter.parse_excel plus the 8 direct parse_sebi_excel callers; validate-when-present semantics means an absent banner behaves exactly as today, and the worst failure mode is a LOUD per-AMC abort on a genuine mismatch (desired; per-AMC isolation in _run.py:221-232 keeps other AMCs and the pipeline running). The step-4 sweep empirically bounds false positives at 0 on the current cache. Next-pipeline-run interaction: ORDER MATTERS — a June `mfs pipeline` run defaults holdings to ym=2026-05 (cli.py:1158, _default_data_month), so purging WITHOUT the code fix would simply re-download the April files and re-poison both cache and DB; with the fix, quant aborts cleanly (Gate B advisory gap) until real May files appear, then ingests normally into the empty cache. Rollback: code revert is a clean git revert (additive kwargs, new exception, one except-clause); the purged DB rows are reconstructable exactly (they equal the 2026-04 partition with as_of_month shifted) or re-created by re-running the quant ingest once May publishes; purged cache files re-download on demand. Invariants respected: no manual data entry, absence-over-wrongness fail-fast, incremental-by-default untouched.
