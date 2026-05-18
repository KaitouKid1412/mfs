"""Verify the manual-CSV loader tolerates niftyindices.com's native format."""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.benchmarks import _ingest_from_manual_csv


@pytest.fixture
def fake_manual_dir(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    (raw / "benchmarks" / "manual").mkdir(parents=True)
    monkeypatch.setenv("MFS_DATA_DIR", str(tmp_path))
    # Clear cached settings/paths
    from mfs import config

    config.get_settings.cache_clear()
    return raw / "benchmarks" / "manual"


def test_native_niftyindices_format(fake_manual_dir: Path):
    """Native CSV: Date,Open,High,Low,Close — DD-Mon-YYYY date."""
    p = fake_manual_dir / "nifty_50_tri.csv"
    p.write_text(
        "Date,Open,High,Low,Close\n"
        "01-Jan-2024,29476.18,29618.65,29457.39,29581.42\n"
        "02-Jan-2024,29600.00,29700.50,29550.00,29650.75\n"
        "03-Jan-2024,29650.75,29800.00,29600.00,29750.30\n"
    )
    df = _ingest_from_manual_csv("NIFTY 50 TRI")
    assert df.height == 3
    assert sorted(df.columns) == ["close", "date"]
    assert df["close"].to_list() == [29581.42, 29650.75, 29750.30]


def test_total_returns_index_column(fake_manual_dir: Path):
    """Some downloads name the value column 'Total Returns Index'."""
    p = fake_manual_dir / "nifty_500_tri.csv"
    p.write_text(
        "Date,Total Returns Index\n"
        "01-Jan-2024,12345.67\n"
        "02-Jan-2024,12400.00\n"
    )
    df = _ingest_from_manual_csv("NIFTY 500 TRI")
    assert df.height == 2
    assert df["close"].to_list() == [12345.67, 12400.00]


def test_thousands_separator(fake_manual_dir: Path):
    """Commas in numbers must be stripped."""
    p = fake_manual_dir / "nifty_100_tri.csv"
    p.write_text("Date,Close\n01-Jan-2024,\"29,476.18\"\n02-Jan-2024,\"29,650.75\"\n")
    df = _ingest_from_manual_csv("NIFTY 100 TRI")
    assert df.height == 2
    assert df["close"].to_list() == [29476.18, 29650.75]
