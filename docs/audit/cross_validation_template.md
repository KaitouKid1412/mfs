# Cross-validation recorded run — <YYYY-MM-DD>

Copy this file to `docs/audit/cross_validation_<YYYY-MM-DD>.md` and fill in
every section. Procedure: `docs/ops/spot_check.md`. Tool:
`tools/cross_validate.py` (read-only).

- Operator:
- Date of run:
- computed_metrics as_of:
- Shortlist run validated (data/output/shortlist/<as_of>):
- git SHA (from that run's manifest.json):

## 1. Fund trailing returns (5 funds, 5 AMCs, tolerance ±0.30pp)

External source per fund: AMC factsheet (note month) or ValueResearch
(note retrieval date). Plans must be Direct-Growth.

| scheme_code | fund | ours 3y | external 3y | delta (pp) | ours 5y | external 5y | delta (pp) | source | verdict |
|---|---|---|---|---|---|---|---|---|---|
|  |  |  |  |  |  |  |  |  |  |
|  |  |  |  |  |  |  |  |  |  |
|  |  |  |  |  |  |  |  |  |  |
|  |  |  |  |  |  |  |  |  |  |
|  |  |  |  |  |  |  |  |  |  |

Deviations + explanations (date-mismatch arithmetic counts as an
explanation; "unknown" does not):

-

## 2. Benchmark TRI levels (exact match expected)

| ticker | date | ours | niftyindices.com | match? |
|---|---|---|---|---|
| NIFTY 50 TRI |  |  |  |  |
| NIFTY 50 TRI |  |  |  |  |
| NIFTY Midcap 150 TRI |  |  |  |  |
| NIFTY Midcap 150 TRI |  |  |  |  |

## 3. Alpha sanity (sign/magnitude only — methodology differs)

| scheme_code | ours alpha_3y_annualized | alpha_confidence | external alpha (source) | plausible? |
|---|---|---|---|---|
|  |  |  |  |  |

## 4. Kotak holdings parity (prodtest-host provenance check)

- Scheme-month checked:
- Top-10 from holdings_monthly vs Kotak public disclosure: identical? (Y/N)
- Divergences:

## Verdict

- [ ] All items within tolerance or explained — shortlist publishable.
- [ ] FAIL — publishing blocked; root-cause notes:
