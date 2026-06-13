# External cross-validation spot-check (F-11)

No computed number leaves this pipeline unreconciled: after every coordinated
recompute (and before publishing any shortlist built on it), run this
checklist with `tools/cross_validate.py` (read-only — it never writes to
Postgres or `data/`). **Any unexplained deviation blocks publishing that
shortlist** (fail-fast invariant).

Record each run from the template at `docs/audit/cross_validation_template.md`
into `docs/audit/cross_validation_<YYYY-MM-DD>.md`.

## Checklist

1. **Five shortlisted funds, five different AMCs.** Take stage-3 picks from
   the latest run (`rank_history` stage 3 or `stage3/mf_report.csv`), then:

   ```bash
   uv run python tools/cross_validate.py --schemes <code1>,...,<code5>
   ```

   Compare the **3y/5y point-to-point CAGR** lines against the AMC
   factsheet's trailing returns or ValueResearch (Direct-Growth plan!).
   Tolerance: **±0.30pp** (publication-date mismatch allowance — our anchor
   is the latest NAV, theirs is usually month-end). The stored
   `ret_3y_median`/`ret_5y_median` are rolling-window medians and are NOT
   expected to match trailing returns; they are printed for context only.
   Optionally feed the hand-entered external values back through
   `--external ext.csv` (columns
   `scheme_code,ext_3y_cagr_pct,ext_5y_cagr_pct,source`) to get automatic
   deltas and PASS/FAIL verdicts.

2. **Two TRI levels on two dates** — NIFTY 50 TRI and NIFTY Midcap 150 TRI:

   ```bash
   uv run python tools/cross_validate.py --ticker "NIFTY 50 TRI" \
       --dates <d1>,<d2>
   ```

   Compare against niftyindices.com historical data. **Exact match
   expected** — any difference means we ingested the wrong series (e.g.
   Price Return instead of Total Return) or a restatement we missed.

3. **Alpha sanity (sign + magnitude only).** External alpha figures use
   different conventions (CAPM vs our rolling signed Jensen's alpha, median
   over all windows, log-basis, T-bill risk-free). Do not expect numeric
   agreement; check that the sign and rough magnitude are not absurd vs
   ValueResearch's alpha for the same fund. Note `alpha_confidence` — low
   confidence means the windows' t-stats are weak and disagreement is
   expected.

4. **Kotak holdings parity (recurring — F-13 provenance risk).** Kotak's
   holdings adapter reads `vlbapiprodtest.kotakmf.com` — a TEST host with no
   freshness/correctness SLA (see `docs/ops/scraping_provenance.md` and the
   `kotak.py` module docstring). Each spot-check run: pick one Kotak ranked
   scheme-month from `holdings_monthly` (read-only psql), pull the top-10
   holdings by weight, and compare names + weights against Kotak's public
   monthly portfolio-disclosure Excel on www.kotakmf.com (browser, manual —
   the page sits behind a bot challenge). Row-identical expected; any
   divergence means the prodtest host has drifted from production and the
   adapter must be re-pointed before the next ingest.

   ```sql
   SELECT security_name, weight_pct FROM holdings_monthly
   WHERE source_amc = 'kotak' AND scheme_code = '<code>'
     AND as_of_month = (SELECT MAX(as_of_month) FROM holdings_monthly
                        WHERE source_amc = 'kotak')
   ORDER BY weight_pct DESC LIMIT 10;
   ```

5. **Record + sign off.** Fill the template, including explanations for any
   in-tolerance-but-nonzero deviations (date mismatch, dividend handling).
   An unexplained FAIL on items 1, 2, or 4 blocks publishing the shortlist
   until root-caused.

## Schedule

- Once after every coordinated recompute (e.g. the Stage-2 A1-10 recompute),
  before the shortlist is treated as publishable.
- The Kotak parity item (4) additionally recurs monthly with the holdings
  ingest cycle.
