"""Verify the parser handles the AMFI bulk-history layout (8 cols, Scheme Name 2nd)."""

from mfs.ingest.amfi_nav import parse_to_dataframe

HISTORY_SAMPLE = """Scheme Code;Scheme Name;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Repurchase Price;Sale Price;Date

Open Ended Schemes ( Equity Scheme - Large Cap Fund )

Aditya Birla Sun Life Mutual Fund

103174;Aditya Birla Sun Life Frontline Equity Fund - Direct Plan - Growth;INF209K01YN1;INF209K01YO9;450.12;;;15-May-2026
103174;Aditya Birla Sun Life Frontline Equity Fund - Direct Plan - Growth;INF209K01YN1;INF209K01YO9;451.30;;;16-May-2026

HDFC Mutual Fund

118989;HDFC Mid-Cap Opportunities Fund - Direct Plan - Growth;INF179K01YE1;INF179K01YF8;150.55;;;15-May-2026
"""


def test_parses_history_layout():
    df = parse_to_dataframe(HISTORY_SAMPLE)
    assert df.height == 3
    row = df.filter(df["scheme_code"] == "103174").sort("nav_date").to_dicts()
    assert len(row) == 2
    assert row[0]["nav"] == 450.12
    assert "Aditya Birla" in row[0]["amc_name"]
    assert row[1]["nav"] == 451.30
