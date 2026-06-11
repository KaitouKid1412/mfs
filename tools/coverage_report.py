"""Coverage report for holdings / constituent-weights / PTR against the
rankable universe. Read-only. Used to track the Phase 5 push to >95%.

Universe = scheme_master rows that are is_active AND DIRECT + GROWTH AND have
a canonical_category. We report two denominators:
  - raw      : every such row (matches the originally-quoted 7.6/22/72%)
  - addressable: raw minus rows that can never carry a domestic Indian
                 holdings/PTR signal — legacy Bonus Options and Segregated
                 Portfolios (share-class duplicates the rank stage dedupes)
                 and overseas/FoF/commodity funds (no Indian equity book).

Constituent-weight coverage is by benchmark ticker: of the distinct
benchmark_ticker values used by the universe, how many have any row in
index_constituents_monthly.

Usage: uv run python tools/coverage_report.py
"""

from __future__ import annotations

from mfs.db.connection import connect

_BASE = (
    "is_active AND plan_type='DIRECT' AND option_type='GROWTH' "
    "AND canonical_category IS NOT NULL"
)
_BONUS_SEG = r"(scheme_name ~* '\mbonus\M' OR scheme_name ~* 'segregated')"
_OVERSEAS = (
    r"scheme_name ~* '(us |u\.s\.|global|overseas|international|nasdaq|"
    r"s&p|hang seng|greater china|china|gold|silver|fof|fund of fund)'"
)


def _scalar(c, sql, args=()):
    return c.execute(sql, args).fetchone()[0]


def main():
    with connect() as c:
        for label, extra in [("raw", ""), ("addressable", f" AND NOT {_BONUS_SEG} AND NOT {_OVERSEAS}")]:
            uni = f"SELECT scheme_code, benchmark_ticker FROM scheme_master WHERE {_BASE}{extra}"
            n = _scalar(c, f"SELECT count(*) FROM ({uni}) u")
            hold = _scalar(c, f"SELECT count(*) FROM ({uni}) u WHERE EXISTS (SELECT 1 FROM holdings_monthly h WHERE h.scheme_code=u.scheme_code)")
            ptr = _scalar(c, f"SELECT count(*) FROM ({uni}) u WHERE EXISTS (SELECT 1 FROM portfolio_turnover_monthly p WHERE p.scheme_code=u.scheme_code)")
            n_tick = _scalar(c, f"SELECT count(DISTINCT benchmark_ticker) FROM ({uni}) u WHERE benchmark_ticker IS NOT NULL")
            tick_cov = _scalar(c, f"SELECT count(DISTINCT benchmark_ticker) FROM ({uni}) u WHERE benchmark_ticker IS NOT NULL AND EXISTS (SELECT 1 FROM index_constituents_monthly i WHERE i.ticker=u.benchmark_ticker)")
            print(f"\n=== {label} universe: {n} funds ===")
            print(f"  Holdings        : {hold:4d}/{n} = {100.0*hold/n:5.1f}%")
            print(f"  PTR             : {ptr:4d}/{n} = {100.0*ptr/n:5.1f}%")
            print(f"  Constituents    : {tick_cov:3d}/{n_tick} tickers = {100.0*tick_cov/n_tick:5.1f}%")

        # Per-AMC remaining gaps on the addressable universe.
        print("\n=== addressable holdings gaps by AMC (top 20) ===")
        rows = c.execute(
            f"""WITH u AS (SELECT scheme_code, amc_code FROM scheme_master
                WHERE {_BASE} AND NOT {_BONUS_SEG} AND NOT {_OVERSEAS})
                SELECT amc_code,
                  count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM holdings_monthly h WHERE h.scheme_code=u.scheme_code)) miss,
                  count(*) total
                FROM u GROUP BY 1 HAVING count(*) FILTER (WHERE NOT EXISTS
                  (SELECT 1 FROM holdings_monthly h WHERE h.scheme_code=u.scheme_code)) > 0
                ORDER BY miss DESC LIMIT 20"""
        ).fetchall()
        for amc, miss, total in rows:
            print(f"  {amc:24s} {miss:3d}/{total}")

        print("\n=== addressable PTR gaps by AMC (top 20) ===")
        rows = c.execute(
            f"""WITH u AS (SELECT scheme_code, amc_code FROM scheme_master
                WHERE {_BASE} AND NOT {_BONUS_SEG} AND NOT {_OVERSEAS})
                SELECT amc_code,
                  count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM portfolio_turnover_monthly p WHERE p.scheme_code=u.scheme_code)) miss,
                  count(*) total
                FROM u GROUP BY 1 HAVING count(*) FILTER (WHERE NOT EXISTS
                  (SELECT 1 FROM portfolio_turnover_monthly p WHERE p.scheme_code=u.scheme_code)) > 0
                ORDER BY miss DESC LIMIT 20"""
        ).fetchall()
        for amc, miss, total in rows:
            print(f"  {amc:24s} {miss:3d}/{total}")

        print("\n=== benchmark tickers WITHOUT constituents (addressable) ===")
        rows = c.execute(
            f"""WITH u AS (SELECT benchmark_ticker, count(*) n FROM scheme_master
                WHERE {_BASE} AND NOT {_BONUS_SEG} AND NOT {_OVERSEAS} AND benchmark_ticker IS NOT NULL
                GROUP BY 1)
                SELECT benchmark_ticker, n FROM u
                WHERE NOT EXISTS (SELECT 1 FROM index_constituents_monthly i WHERE i.ticker=u.benchmark_ticker)
                ORDER BY n DESC"""
        ).fetchall()
        for t, n in rows:
            print(f"  {t:34s} {n:3d} funds")


if __name__ == "__main__":
    main()
