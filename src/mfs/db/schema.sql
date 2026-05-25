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
    last_seen_date      DATE NOT NULL
);
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

-- Phase 2.3: per-scheme monthly AUM in INR Crore. Extracted from the same
-- factsheet PDFs as holdings/PTR/managers.
CREATE TABLE IF NOT EXISTS scheme_aum_monthly (
    scheme_code         TEXT             NOT NULL,
    as_of_month         DATE             NOT NULL,
    aum_crore           DOUBLE PRECISION NOT NULL,  -- INR Crore (1 Cr = 10M INR)
    source_amc          TEXT             NOT NULL,
    computed_at         TIMESTAMP        NOT NULL,
    PRIMARY KEY (scheme_code, as_of_month)
);
CREATE INDEX IF NOT EXISTS idx_aum_scheme ON scheme_aum_monthly (scheme_code);

