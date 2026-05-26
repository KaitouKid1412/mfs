"""Tests for the ITI Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/iti/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR extraction. The URL
resolver test patches the live API helper to avoid network I/O.

Notes on the unusual fetch path: ITI's CMS prepends an opaque
upload-time epoch prefix to the factsheet filename
(``1779445747-ITI_Factsheet_April_26.pdf``), so the URL cannot be
constructed from ``ym`` alone. ``build_url(ym)`` performs a live API
lookup; we patch the helper here for determinism.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mfs.errors import IngestError
from mfs.ingest.managers.iti import (
    ItiAdapter,
    _aes_decrypt,
    _aes_encrypt,
    _filename_month_label,
    _scheme_name_from_page,
)

PDF = Path(__file__).parent.parent / "data" / "raw" / "factsheets" / "iti" / "2026-04.pdf"


# ---------------------------------------------------------------------------
# build_url — patched API
# ---------------------------------------------------------------------------


def test_build_url_april_2026():
    """``build_url`` looks up the canonical PDF URL via the AMC's
    encrypted API. We patch the resolver to return the exact URL we
    downloaded from for calibration.
    """
    expected = "https://itiamc.com/admin/pdf/1779445747-ITI_Factsheet_April_26.pdf"
    with patch("mfs.ingest.managers.iti._resolve_pdf_url", return_value=expected) as mock:
        url = ItiAdapter().build_url("2026-04")
    assert url == expected
    mock.assert_called_once_with("2026-04")


def test_build_url_raises_when_not_published():
    """If the API has no entry for the data month, the resolver raises
    IngestError — consistent with the fail-fast invariant."""
    def _boom(ym: str) -> str:
        raise IngestError(f"iti: no factsheet URL published for data month {ym!r}")

    with patch("mfs.ingest.managers.iti._resolve_pdf_url", side_effect=_boom):
        with pytest.raises(IngestError):
            ItiAdapter().build_url("2030-09")


# ---------------------------------------------------------------------------
# Helper unit tests (no network, no PDF)
# ---------------------------------------------------------------------------


def test_filename_month_label():
    """The resolver matches API entries by ``Factsheet - <Month> <YYYY>``;
    the helper produces the ``<Month> <YYYY>`` token."""
    assert _filename_month_label("2026-04") == "April 2026"
    assert _filename_month_label("2025-12") == "December 2025"
    assert _filename_month_label("2026-01") == "January 2026"


def test_aes_roundtrip():
    """Sanity-check the AES-128-CBC roundtrip against the hard-coded
    key / IV extracted from the ITI Angular bundle.
    """
    pt = '{"year":"2026","guid":"abc","timeStamp":1748175000000}'
    ct = _aes_encrypt(pt)
    assert _aes_decrypt(ct) == pt


def test_scheme_name_gates_on_markers():
    """The scheme-name detector requires BOTH ``CATEGORY OF SCHEME`` and
    ``PORTFOLIO DETAILS`` markers — they're present on every scheme
    detail page and absent everywhere else.
    """
    body = "ITI Multi Cap Fund\nCATEGORY OF SCHEME MULTICAP FUND\nPORTFOLIO DETAILS\n..."
    assert _scheme_name_from_page(body) == "ITI Multi Cap Fund"
    # Missing either marker -> None.
    assert _scheme_name_from_page("ITI Multi Cap Fund\nCATEGORY OF SCHEME MULTICAP FUND") is None
    assert _scheme_name_from_page("ITI Multi Cap Fund\nPORTFOLIO DETAILS") is None
    # Ready Reckoner pages are excluded even when both markers appear in
    # the dump (they don't in practice — guard is defensive).
    rr = (
        "Equity Funds Ready Reckoner\nITI Multi Cap Fund\n"
        "CATEGORY OF SCHEME MULTICAP FUND\nPORTFOLIO DETAILS\n"
    )
    assert _scheme_name_from_page(rr) is None


def test_scheme_name_scans_past_chart_banner():
    """Page 20 of the calibration PDF renders the ``ITI Large & Mid Cap
    Fund`` title AFTER a 30-line chart-axis banner. The detector must
    keep scanning until it finds an ``ITI ... Fund`` line.
    """
    chart_banner = "\n".join(["April 2026", "Fund vs Index Overweight / Underweight"] + ["0.00"] * 27)
    body = (
        f"{chart_banner}\n"
        "ITI Large & Mid Cap Fund\n"
        "CATEGORY OF SCHEME LARGE & MID CAP FUND\n"
        "PORTFOLIO DETAILS\n"
    )
    assert _scheme_name_from_page(body) == "ITI Large & Mid Cap Fund"


# ---------------------------------------------------------------------------
# PTR extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> ItiAdapter:
    return ItiAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: ItiAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached ITI PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# ----- PTR --------------------------------------------------------------


def test_parse_ptr_yields_records(ptr_records):
    """ITI publishes PTR for 11 equity schemes (Multi Cap, ELSS, Large,
    Mid, Small, Value, Pharma, Banking, Flexi, Focused, Large & Mid) +
    1 hybrid (Balanced Advantage) = 12 rows on 2026-04. Brief target:
    >= 11."""
    assert len(ptr_records) >= 11


def test_parse_ptr_known_scheme_multi_cap(ptr_records):
    """ITI Multi Cap Fund prints ``Portfolio Turnover Ratio 1.08`` on
    page 10 of the April-2026 PDF. Stored as fraction (= 108%
    turnover)."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "ITI Multi Cap Fund" in by_name
    rec = by_name["ITI Multi Cap Fund"]
    assert rec.ptr == pytest.approx(1.08, abs=1e-6)
    assert rec.source_amc == "iti"


def test_parse_ptr_known_scheme_pharma(ptr_records):
    """ITI Pharma and Healthcare Fund PTR = 0.41 — exercises the
    multi-word scheme name on page 16."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "ITI Pharma and Healthcare Fund" in by_name
    assert by_name["ITI Pharma and Healthcare Fund"].ptr == pytest.approx(0.41, abs=1e-6)


def test_parse_ptr_known_scheme_large_and_mid(ptr_records):
    """ITI Large & Mid Cap Fund on page 20 has its title after a chart
    banner — exercises the scan-past-chart logic. PTR = 1.24."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "ITI Large & Mid Cap Fund" in by_name
    assert by_name["ITI Large & Mid Cap Fund"].ptr == pytest.approx(1.24, abs=1e-6)


def test_parse_ptr_drops_newly_launched_schemes(ptr_records):
    """ITI Bharat Consumption Fund (page 21) prints ``Portfolio
    Turnover Ratio -`` and ITI Business Cycle Fund (page 22) prints
    ``Portfolio Turnover Ratio NA`` — both are < 1 year old with a
    "Since the scheme has not completed one year" footnote. The
    fail-fast invariant requires we drop them rather than fabricate a
    value.
    """
    names = {r.scheme_name_printed for r in ptr_records}
    assert "ITI Bharat Consumption Fund" not in names
    assert "ITI Business Cycle Fund" not in names


def test_parse_ptr_values_are_fraction(ptr_records):
    """ITI prints PTR as a fraction ('times' convention). Storage is
    also fraction, so no /100. Guard against an accidental ×100
    encoding (would push values above 100)."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 100, r


# ----- AUM --------------------------------------------------------------


