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


NONPOSITIVE_SAMPLE = SAMPLE + (
    "148265;INF204KB1C90;INF204KB1D08;Nippon India Segregated Portfolio 2;0.00000;15-May-2026\n"
    "148266;INF204KB1C91;INF204KB1D09;Bogus Negative NAV Fund - Direct - Growth;-12.5000;15-May-2026\n"
)


def test_skips_nonpositive_navs_and_counts_them(monkeypatch):
    import mfs.ingest.amfi_nav as amfi_nav

    warnings: list[tuple] = []

    class _Log:
        def warning(self, event, **kw):
            warnings.append((event, kw))

        def info(self, *a, **kw):
            pass

        def error(self, *a, **kw):
            pass

    monkeypatch.setattr(amfi_nav, "log", _Log())
    df = parse_to_dataframe(NONPOSITIVE_SAMPLE)
    assert df.height == 3  # zero-NAV and negative-NAV rows dropped
    assert "148265" not in df["scheme_code"].to_list()
    assert "148266" not in df["scheme_code"].to_list()
    assert (df["nav"] > 0).all()
    skip_events = [kw for ev, kw in warnings if ev == "amfi_nav.nonpositive_nav_skipped"]
    assert skip_events == [{"n": 2}]


def test_no_skip_warning_when_all_navs_positive(monkeypatch):
    import mfs.ingest.amfi_nav as amfi_nav

    warnings: list[tuple] = []

    class _Log:
        def warning(self, event, **kw):
            warnings.append((event, kw))

    monkeypatch.setattr(amfi_nav, "log", _Log())
    df = parse_to_dataframe(SAMPLE)
    assert df.height == 3
    assert warnings == []
