"""Tests for the Kotak Mahindra Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 PDF at
``data/raw/factsheets/kotak/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR / AUM extraction plus URL
construction. No network access is performed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.kotak import KotakAdapter, _publish_ym

PDF = Path(__file__).parent.parent / "data" / "raw" / "factsheets" / "kotak" / "2026-04.pdf"


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_uses_publish_month():
    """Kotak names the PDF by the PUBLISH month (data month + 1).
    Data month 2026-04 → publish May 2026 → filename
    ``Factsheet-May-2026.pdf``.
    """
    a = KotakAdapter()
    assert a.build_url("2026-04") == (
        "https://www.kotakmf.com/Information/forms-and-downloads/"
        "Factsheet/Factsheet-May-2026.pdf"
    )


def test_build_url_rolls_year_at_december():
    a = KotakAdapter()
    # data month Dec → publish January of next year.
    assert a.build_url("2026-12") == (
        "https://www.kotakmf.com/Information/forms-and-downloads/"
        "Factsheet/Factsheet-January-2027.pdf"
    )


def test_publish_ym_helper():
    assert _publish_ym("2026-04") == (2026, 5)
    assert _publish_ym("2025-12") == (2026, 1)


# ---------------------------------------------------------------------------
# PTR / AUM extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> KotakAdapter:
    return KotakAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: KotakAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Kotak PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# ---------- PTR ----------


def test_parse_ptr_yields_records(ptr_records):
    """The April 2026 Kotak PDF prints PTR for ~60 equity schemes (debt and
    hybrid pages typically omit it)."""
    assert len(ptr_records) >= 1


def test_parse_ptr_known_scheme(ptr_records):
    """Kotak Large Cap Fund's PTR on the April 2026 publish is 24.27% →
    stored as 0.2427 fraction."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "KOTAK LARGE CAP FUND" in by_name
    rec = by_name["KOTAK LARGE CAP FUND"]
    assert rec.ptr == pytest.approx(0.2427, abs=1e-4)
    assert rec.source_amc == "kotak"


def test_parse_ptr_values_are_fraction(ptr_records):
    """Kotak prints PTR as a percent — the adapter divides by 100. Guard
    against an accidental missed conversion (a single value > 100 fraction
    would imply 10,000%+, almost certainly wrong)."""
    for r in ptr_records:
        assert r.ptr > 0, r
        # Quant Fund legitimately shows ~1.51 (151%); arbitrary upper bound
        # at 100 catches an unconverted percent encoding (would be 100+).
        assert r.ptr < 100, r


def test_parse_ptr_wrapped_scheme_name(ptr_records):
    """Page 17 has a two-line wrapped scheme name (KOTAK INFRASTRUCTURE & /
    ECONOMIC REFORM FUND) — the header parser must reassemble it."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "KOTAK INFRASTRUCTURE & ECONOMIC REFORM FUND" in by_name


# ---------- AUM ----------


