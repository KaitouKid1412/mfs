-- mfs Postgres schema. Idempotent: CREATE TABLE IF NOT EXISTS.
-- Run via `mfs db init`. All curated/metrics datasets live here; raw archives
-- (raw HTTP responses, AMFI history dumps, RBI press releases) stay on disk
-- under data/raw/ for re-derivation and audit purposes.

CREATE TABLE IF NOT EXISTS nav_daily (
    scheme_code     TEXT             NOT NULL,
    nav_date        DATE             NOT NULL,
    nav             DOUBLE PRECISION NOT NULL,
    isin_growth     TEXT,
    isin_idcw       TEXT,
    scheme_name     TEXT,
    amc_name        TEXT,
    amfi_category   TEXT,
    PRIMARY KEY (scheme_code, nav_date)
);

-- A1-1: zero/negative NAVs are rejected at ingest (amfi_nav.parse_amfi_text
-- skips them) and refused at the DB boundary by this CHECK. Added NOT VALID
-- so `mfs db init` can run BEFORE the legacy zero-NAV rows are deleted (the
-- constraint still applies to all NEW writes immediately). After the cleanup
-- DELETE, run:
--   ALTER TABLE nav_daily VALIDATE CONSTRAINT chk_nav_positive;
-- Note: re-running db init re-creates the constraint as NOT VALID; new writes
-- stay checked either way — re-VALIDATE to re-assert the whole-table guarantee.
ALTER TABLE nav_daily DROP CONSTRAINT IF EXISTS chk_nav_positive;
ALTER TABLE nav_daily ADD CONSTRAINT chk_nav_positive CHECK (nav > 0) NOT VALID;

CREATE TABLE IF NOT EXISTS benchmark_daily (
    ticker          TEXT             NOT NULL,
    date            DATE             NOT NULL,
    close           DOUBLE PRECISION NOT NULL,
    is_synthetic    BOOLEAN          NOT NULL DEFAULT FALSE,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS risk_free_daily (
    date            DATE             PRIMARY KEY,
    rate_annual     DOUBLE PRECISION NOT NULL,
    rate_daily      DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS scheme_master (
    scheme_code         TEXT PRIMARY KEY,
    isin_growth         TEXT,
    isin_idcw           TEXT,
    scheme_name         TEXT NOT NULL,
    amc_name            TEXT NOT NULL,
    amc_code            TEXT NOT NULL,
    plan_type           TEXT NOT NULL,
    option_type         TEXT NOT NULL,
    amfi_category       TEXT,
    canonical_category  TEXT,
    benchmark_ticker    TEXT,
    inception_date      DATE,
    base_fund_id        TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    last_seen_date      DATE NOT NULL,
    departed_at         DATE
);
-- D1 survivorship containment: scheme_master is diff-synced, never truncated.
-- departed_at records the first run date a scheme was absent from the AMFI
-- NAVAll listing; NULL while active, cleared again if the scheme re-appears.
ALTER TABLE scheme_master ADD COLUMN IF NOT EXISTS departed_at DATE;
CREATE INDEX IF NOT EXISTS idx_scheme_master_active_direct_growth
    ON scheme_master (canonical_category)
    WHERE is_active AND plan_type = 'DIRECT' AND option_type = 'GROWTH';

CREATE TABLE IF NOT EXISTS computed_metrics (
    as_of_date              DATE NOT NULL,
    scheme_code             TEXT NOT NULL,
    canonical_category      TEXT,
    benchmark_ticker        TEXT,
    ret_3y_median           DOUBLE PRECISION,
    ret_3y_p25              DOUBLE PRECISION,
    ret_5y_median           DOUBLE PRECISION,
    ret_5y_p25              DOUBLE PRECISION,
    alpha_3y_annualized     DOUBLE PRECISION,
    alpha_3y_tstat          DOUBLE PRECISION,
    sortino_3y              DOUBLE PRECISION,
    info_ratio_3y           DOUBLE PRECISION,
    capture_up              DOUBLE PRECISION,
    capture_down            DOUBLE PRECISION,
    capture_efficiency      DOUBLE PRECISION,
    r_squared_3y            DOUBLE PRECISION,
    beta_3y                 DOUBLE PRECISION,
    data_quality_flag       TEXT NOT NULL,
    computed_at             TIMESTAMP NOT NULL,
    pipeline_version        TEXT NOT NULL,
    PRIMARY KEY (as_of_date, scheme_code)
);
CREATE INDEX IF NOT EXISTS idx_computed_metrics_category
    ON computed_metrics (as_of_date, canonical_category);

-- Per-scheme daily log return cache. Populated incrementally from nav_daily.
-- Compute layer reads from here instead of re-deriving log returns on every run.
CREATE TABLE IF NOT EXISTS fund_log_returns (
    scheme_code     TEXT             NOT NULL,
    date            DATE             NOT NULL,
    log_return      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (scheme_code, date)
);

-- Phase 2: additive columns on computed_metrics. All nullable; Phase 2.0 populates
-- only beta_3y_std, r_squared_3y_mean, and the derived style_drift_3y. The remaining
-- columns are filled by Phase 2.1-2.3 ingestion stages.
ALTER TABLE computed_metrics ADD COLUMN IF NOT EXISTS beta_3y_std            DOUBLE PRECISION;
ALTER TABLE computed_metrics ADD COLUMN IF NOT EXISTS r_squared_3y_mean      DOUBLE PRECISION;
ALTER TABLE computed_metrics ADD COLUMN IF NOT EXISTS style_drift_3y         DOUBLE PRECISION;
ALTER TABLE computed_metrics ADD COLUMN IF NOT EXISTS active_share_median_1y DOUBLE PRECISION;
ALTER TABLE computed_metrics ADD COLUMN IF NOT EXISTS ptr_latest             DOUBLE PRECISION;
ALTER TABLE computed_metrics ADD COLUMN IF NOT EXISTS aum_impact_cost_days   DOUBLE PRECISION;

-- Phase 2.3: scheme AUM diagnostic surfaced into computed_metrics.
ALTER TABLE computed_metrics ADD COLUMN IF NOT EXISTS scheme_aum_crore       DOUBLE PRECISION;

-- Manager-tenure (Phase 2.1) and SEBI stress-test (Phase 2.3) ingest paths
-- and their computed_metrics columns were removed when the user opted to
-- verify those signals manually for the Stage 2 survivor set. The columns
-- may still exist on legacy DB rows but the active code no longer reads or
-- writes them.

-- Phase 2.2: monthly portfolio holdings extracted from AMC factsheets.
-- Indian AMC factsheets DO NOT print ISINs in their portfolio listings — only
-- company name, industry, and weight. The primary key is therefore
-- (scheme_code, security_name, as_of_month). ISIN is optional and gets
-- backfilled at compute time by joining to index_constituents_monthly on
-- normalized security_name.
--
-- weight_pct is in percent (0.0-100.0). instrument_type is the normalized
-- AMC-printed section label (Equity / Debt / Cash / Derivative / REIT / ...).
-- Active Share filters to instrument_type='Equity' before computing.
CREATE TABLE IF NOT EXISTS holdings_monthly (
    scheme_code         TEXT             NOT NULL,
    security_name       TEXT             NOT NULL,
    as_of_month         DATE             NOT NULL,  -- first day of month
    weight_pct          DOUBLE PRECISION NOT NULL,
    isin                TEXT,  -- nullable; populated when known
    instrument_type     TEXT             NOT NULL,
    source_amc          TEXT             NOT NULL,
    computed_at         TIMESTAMP        NOT NULL,
    PRIMARY KEY (scheme_code, security_name, as_of_month)
);
CREATE INDEX IF NOT EXISTS idx_holdings_scheme_month
    ON holdings_monthly (scheme_code, as_of_month);

-- Phase 2.2: monthly benchmark constituents (Nifty 100 TRI, Nifty 500 TRI, etc.).
-- Sourced from niftyindices.com per-index CSVs with manual-CSV fallback for
-- synthetic/hybrid TRIs (Aggressive Hybrid TRI, Conservative Hybrid TRI, ...).
CREATE TABLE IF NOT EXISTS index_constituents_monthly (
    ticker              TEXT             NOT NULL,
    isin                TEXT             NOT NULL,
    as_of_month         DATE             NOT NULL,
    weight_pct          DOUBLE PRECISION NOT NULL,
    security_name       TEXT,
    PRIMARY KEY (ticker, isin, as_of_month)
);
CREATE INDEX IF NOT EXISTS idx_constituents_ticker_month
    ON index_constituents_monthly (ticker, as_of_month);

-- Phase 2.2: monthly portfolio turnover ratios extracted from the same
-- factsheet PDFs as holdings. One number per scheme per month.
CREATE TABLE IF NOT EXISTS portfolio_turnover_monthly (
    scheme_code         TEXT             NOT NULL,
    as_of_month         DATE             NOT NULL,
    ptr                 DOUBLE PRECISION NOT NULL,  -- as a fraction, e.g. 1.27 = 127%
    source_amc          TEXT             NOT NULL,
    computed_at         TIMESTAMP        NOT NULL,
    PRIMARY KEY (scheme_code, as_of_month)
);
CREATE INDEX IF NOT EXISTS idx_ptr_scheme ON portfolio_turnover_monthly (scheme_code);

-- Phase 2.3: NSE daily bhavcopy — close + total traded value per stock.
-- Used to compute the trailing-60-day median Average Daily Volume (ADV) in
-- INR for the AUM Impact Cost calculation. Symbol+ISIN both retained because
-- the bhavcopy keys on symbol, but our holdings table keys on security_name.
-- The orchestrator joins via symbol/ISIN where available; security_name
-- fuzzy-match elsewhere.
CREATE TABLE IF NOT EXISTS stock_adv_daily (
    isin                TEXT             NOT NULL,
    symbol              TEXT,
    date                DATE             NOT NULL,
    close               DOUBLE PRECISION,
    total_traded_value  DOUBLE PRECISION NOT NULL,  -- in INR
    PRIMARY KEY (isin, date)
);
CREATE INDEX IF NOT EXISTS idx_stock_adv_date ON stock_adv_daily (date);

-- Phase 2.3: per-scheme quarterly AUM in INR Crore.
-- Sourced exclusively from AMFI's quarterly AAUM endpoint (see
-- mfs.ingest.amfi_aum). The legacy factsheet-extraction path was removed
-- and the CHECK constraint enforces the single-source invariant — any
-- attempt to write with a different source_amc will be rejected.
CREATE TABLE IF NOT EXISTS scheme_aum_monthly (
    scheme_code         TEXT             NOT NULL,
    as_of_month         DATE             NOT NULL,
    aum_crore           DOUBLE PRECISION NOT NULL,  -- INR Crore (1 Cr = 10M INR)
    source_amc          TEXT             NOT NULL,
    computed_at         TIMESTAMP        NOT NULL,
    PRIMARY KEY (scheme_code, as_of_month),
    CONSTRAINT chk_scheme_aum_source_amfi CHECK (source_amc = 'amfi_aaum')
);
CREATE INDEX IF NOT EXISTS idx_aum_scheme ON scheme_aum_monthly (scheme_code);

-- D2 point-in-time snapshots: one full copy of scheme_master per build() run,
-- keyed by snapshot_date. Captures universe membership, plan/option/category
-- assignments AND the benchmark map (benchmark_ticker) as of each run, so
-- backtests and run-diffs can reconstruct any past universe with a plain SQL
-- filter. Includes the inactive/departed rows — they are part of the
-- point-in-time state. Re-running build() on the same date replaces the
-- partition (see writers.snapshot_scheme_master). Derived artifact: no
-- coverage contract (listed in coverage.OUT_OF_CONTRACT).
CREATE TABLE IF NOT EXISTS scheme_master_history (
    snapshot_date       DATE NOT NULL,
    scheme_code         TEXT NOT NULL,
    isin_growth         TEXT,
    isin_idcw           TEXT,
    scheme_name         TEXT NOT NULL,
    amc_name            TEXT NOT NULL,
    amc_code            TEXT NOT NULL,
    plan_type           TEXT NOT NULL,
    option_type         TEXT NOT NULL,
    amfi_category       TEXT,
    canonical_category  TEXT,
    benchmark_ticker    TEXT,
    inception_date      DATE,
    base_fund_id        TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL,
    last_seen_date      DATE NOT NULL,
    departed_at         DATE,
    PRIMARY KEY (snapshot_date, scheme_code)
);

-- D2 point-in-time snapshots: per-run rank outcomes for all three stages,
-- keyed by as_of_date. One row per (as_of_date, stage, scheme_code): included
-- rows carry the flat z-scores, composite score(s) and rank mirrored from the
-- stage CSVs; excluded rows carry included=false plus an exclusion_reason from
-- the cross-workstream contract vocabulary:
--   INSUFFICIENT_HISTORY | MISSING_CORE_METRIC:<name> | STALE_NAV | FILTER:<name>
-- Re-running the same as_of replaces the partition — DELETE + insert in one
-- transaction (see writers.persist_rank_history). run_id stays NULL until the
-- workstream-F run-manifest populates it; the key remains
-- (as_of_date, stage, scheme_code). Derived artifact: no coverage contract
-- (listed in coverage.OUT_OF_CONTRACT).
CREATE TABLE IF NOT EXISTS rank_history (
    as_of_date               DATE NOT NULL,
    stage                    SMALLINT NOT NULL,
    scheme_code              TEXT NOT NULL,
    canonical_category       TEXT,
    composite_score          DOUBLE PRECISION,
    composite_score_stage1   DOUBLE PRECISION,
    rank_in_category         INTEGER,
    z_ret_3y_median          DOUBLE PRECISION,
    z_ret_3y_p25             DOUBLE PRECISION,
    z_ret_5y_median          DOUBLE PRECISION,
    z_ret_5y_p25             DOUBLE PRECISION,
    z_alpha_3y_annualized    DOUBLE PRECISION,
    z_sortino_3y             DOUBLE PRECISION,
    z_info_ratio_3y          DOUBLE PRECISION,
    z_capture_efficiency     DOUBLE PRECISION,
    z_active_share_median_1y DOUBLE PRECISION,
    z_style_drift_3y         DOUBLE PRECISION,
    ptr_latest               DOUBLE PRECISION,
    aum_impact_cost_days     DOUBLE PRECISION,
    included                 BOOLEAN NOT NULL,
    exclusion_reason         TEXT,
    run_id                   TEXT,
    PRIMARY KEY (as_of_date, stage, scheme_code),
    CONSTRAINT chk_rank_history_stage CHECK (stage IN (1, 2, 3))
);
CREATE INDEX IF NOT EXISTS idx_rank_history_category
    ON rank_history (as_of_date, canonical_category);

