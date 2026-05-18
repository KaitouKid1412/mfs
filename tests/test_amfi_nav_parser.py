from mfs.ingest.amfi_nav import parse_to_dataframe

SAMPLE = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Large Cap Fund)

Aditya Birla Sun Life Mutual Fund

103174;INF209K01YN1;INF209K01YO9;Aditya Birla Sun Life Frontline Equity Fund - Direct Plan - Growth;450.1234;15-May-2026
103173;INF209K01YP6;INF209K01YQ4;Aditya Birla Sun Life Frontline Equity Fund - Regular Plan - Growth;410.9876;15-May-2026

Open Ended Schemes(Equity Scheme - Mid Cap Fund)

HDFC Mutual Fund

118989;INF179K01YE1;INF179K01YF8;HDFC Mid-Cap Opportunities Fund - Direct Plan - Growth;150.5500;15-May-2026
"""


def test_parses_amc_category_data_rows():
    df = parse_to_dataframe(SAMPLE)
    assert df.height == 3
    assert set(df.columns) >= {"scheme_code", "scheme_name", "nav", "nav_date", "amc_name", "amfi_category"}
    row = df.filter(df["scheme_code"] == "103174").to_dicts()[0]
    assert row["nav"] == 450.1234
    assert row["amc_name"] == "Aditya Birla Sun Life Mutual Fund"
    assert "Large Cap" in row["amfi_category"]
    hdfc = df.filter(df["scheme_code"] == "118989").to_dicts()[0]
    assert hdfc["amc_name"] == "HDFC Mutual Fund"
    assert "Mid Cap" in hdfc["amfi_category"]


def test_skips_malformed_lines():
    bad = SAMPLE + "9999;;;garbage;N.A.;not-a-date\n"
    df = parse_to_dataframe(bad)
    assert df.height == 3  # malformed row dropped
