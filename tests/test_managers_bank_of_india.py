"""Tests for the Bank of India Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/bank_of_india/2026-04.pdf``. Tests run entirely
offline — they read the bundled PDF and exercise PTR extraction.

``build_url`` is deterministic (slug-stable URL pattern) so no network
stub is needed; the AJAX-fallback resolver is invoked only by ``fetch``
on canonical-URL 404, which we don't exercise here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.bank_of_india import (
    BankOfIndiaAdapter,
    _PTR_RE,
    _canonical_factsheet_url,
)

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "bank_of_india"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url (pure function — no network)
# ---------------------------------------------------------------------------


def test_build_url_april_2026_is_canonical():
    """``build_url('2026-04')`` returns the canonical slug-stable URL —
    the value actually downloaded from boimf.in. The Sitefinity
    ``?sfvrsn=`` cache-buster is intentionally omitted; the bare URL
    serves the same PDF (verified by HEAD probe)."""
    expected = (
        "https://www.boimf.in/docs/default-source/investorcorner/"
        "factsheets/factsheet-april-2026.pdf"
    )
    adapter = BankOfIndiaAdapter()
    assert adapter.build_url("2026-04") == expected
    # Helper produces the same result.
    assert _canonical_factsheet_url("2026-04") == expected


def test_build_url_month_name_lowercase():
    """The path embeds the lower-case English month name. Verify with a
    different month so the regex test doesn't rely on cached state."""
    adapter = BankOfIndiaAdapter()
    assert adapter.build_url("2025-12").endswith(
        "factsheet-december-2025.pdf"
    )
    assert adapter.build_url("2026-01").endswith(
        "factsheet-january-2026.pdf"
    )


def test_amc_slug_matches_scheme_master_exactly():
    """``amc_slug = 'bank_of_india'`` matches ``scheme_master.amc_code``
    verbatim; no alias entry in ``_scheme_match`` is required."""
    adapter = BankOfIndiaAdapter()
    assert adapter.amc_slug == "bank_of_india"


# ---------------------------------------------------------------------------
# Pure regex unit tests (no I/O)
# ---------------------------------------------------------------------------


def test_ptr_regex_matches_clean_line():
    """The textbook layout — ``PORTFOLIO TURNOVER RATIO (As on April 30,
    2026)`` immediately followed by ``<num> Times#`` — should match and
    capture the fraction."""
    text = (
        "PORTFOLIO TURNOVER RATIO (As on April 30, 2026)\n"
        "0.82 Times# (#Basis last rolling 12 months)\n"
    )
    m = _PTR_RE.search(text)
    assert m is not None
    assert float(m.group(1)) == pytest.approx(0.82, abs=1e-6)


def test_ptr_regex_tolerates_column_bleed():
    """On equity scheme pages the value column-bleeds with the parallel
    Investment Objective paragraph, so the literal-text distance between
    the label and the numeric token can exceed 100 chars and crosses a
    newline. The regex skips that bleed via a DOTALL ``.{0,250}?`` gap."""
    text = (
        "INVESTMENT OBJECTIVE PORTFOLIO TURNOVER RATIO (As on April 30, 2026)\n"
        "The investment objective of the scheme is to generate long term "
        "capital appreciation by investing 0.82 Times# (#Basis last rolling 12 months)\n"
        "predominantly in equity and equity-related securities.\n"
    )
    m = _PTR_RE.search(text)
    assert m is not None
    assert float(m.group(1)) == pytest.approx(0.82, abs=1e-6)


def test_ptr_regex_does_not_match_when_label_absent():
    """Debt / liquid / arbitrage pages legitimately omit the PORTFOLIO
    TURNOVER RATIO block — the extractor must return no match (fail-fast:
    no fabricated zeros)."""
    text = (
        "AVERAGE AUM\n` 1,854.30 Crs.\nLATEST AUM\n` 1,734.94 Crs.\n"
        "EXPENSE RATIO Regular Plan: 0.30%\n"
    )
    assert _PTR_RE.search(text) is None


# ---------------------------------------------------------------------------
# PTR extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> BankOfIndiaAdapter:
    return BankOfIndiaAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: BankOfIndiaAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Bank of India PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# ----- PTR --------------------------------------------------------------


def test_parse_ptr_yields_records(ptr_records):
    """Bank of India publishes PTR for its 13 equity scheme pages
    (pages 7-19 on the April-2026 PDF). Brief target: >= 12 rows
    parsed (matcher dedupe may collapse a few). At least 1 row must
    be yielded — brief says SKIP only if PTR is not published, which
    is not the case here."""
    assert len(ptr_records) >= 1
    assert len(ptr_records) >= 12


def test_parse_ptr_known_scheme_flexi_cap(ptr_records):
    """Bank of India Flexi Cap Fund prints ``0.82 Times#`` on page 7 of
    the April-2026 PDF. Stored as a fraction (= 0.82, no /100
    conversion)."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Bank of India Flexi Cap Fund" in by_name
    rec = by_name["Bank of India Flexi Cap Fund"]
    assert rec.ptr == pytest.approx(0.82, abs=1e-6)
    assert rec.source_amc == "bank_of_india"


def test_parse_ptr_known_scheme_small_cap(ptr_records):
    """Bank of India Small Cap Fund — 0.74 Times# — second equity page."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Bank of India Small Cap Fund" in by_name
    assert by_name["Bank of India Small Cap Fund"].ptr == pytest.approx(
        0.74, abs=1e-6
    )


def test_parse_ptr_skips_pure_debt_schemes(ptr_records):
    """Liquid / Short Term Income / Ultra Short Duration / Money Market /
    Credit Risk / Overnight pages legitimately omit the PTR label.
    Balanced Advantage / Conservative Hybrid / Arbitrage also omit it on
    Bank of India's layout. They must NOT appear in PTR rows."""
    names = {r.scheme_name_printed for r in ptr_records}
    assert "Bank of India Liquid Fund" not in names
    assert "Bank of India Short Term Income Fund" not in names
    assert "Bank of India Ultra Short Duration Fund" not in names
    assert "Bank of India Money Market Fund" not in names
    assert "Bank of India Overnight Fund" not in names
    assert "Bank of India Balanced Advantage Fund" not in names
    assert "Bank of India Conservative Hybrid Fund" not in names
    assert "Bank of India Arbitrage Fund" not in names


def test_parse_ptr_values_are_fraction(ptr_records):
    """Bank of India prints PTR in 'Times' (a fraction). Storage is also
    fraction, so no scaling. Sanity-bound to (0, 100) to guard against
    an accidental ×100 percent encoding."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 100, r
