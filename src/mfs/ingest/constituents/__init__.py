"""NSE Index Constituents ingestion (Phase 2.2).

NSE doesn't publish machine-readable constituent **weights** for its indices
(only membership lists via niftyindices.com/IndexConstituent/ind_*list.csv,
which lack a weight column). For Phase 2.2 the primary source is user-
provided monthly weight CSVs under `data/raw/index_constituents/manual/`.
The pipeline reads those CSVs and writes to `index_constituents_monthly`.

CSV format (header row required):
    isin,weight_pct,security_name
    INE002A01018,9.81,Reliance Industries Ltd.
    INE040A01034,7.84,HDFC Bank Ltd.
    ...

One file per (ticker, data_month). File naming:
    data/raw/index_constituents/manual/{ticker_slug}/{YYYY-MM}.csv

Where `ticker_slug` is the canonical lowercase/underscore form of the
benchmark ticker (e.g. 'nifty_100_tri' for 'NIFTY 100 TRI'). See
mfs.paths.ticker_slug().
"""

from __future__ import annotations

from mfs.ingest.constituents._run import run_all, run_for_ticker
from mfs.ingest.constituents.derive import derive_latest_month, derive_month

__all__ = ["run_all", "run_for_ticker", "derive_latest_month", "derive_month"]
