"""CI-runnable adapter value-pinning tests against committed PDF extracts.

F-8 (docs/phase6/stage2_full_remediation.md): the full per-AMC value-pin
suites (tests/test_managers_*.py) read 2-4MB cached factsheets under
gitignored data/raw and are auto-marked ``local_data`` (deselected in CI).
To keep the real parse paths exercised on every CI run, this module pins
one known PTR value per migrated adapter against small page extracts
committed under tests/fixtures/factsheets/ (built with
tools/make_fixture_pdf.py — font programs and images stripped, text layer
intact, so parse output is identical to the full factsheet page).

This module must NOT acquire the ``local_data`` marker: it only reads
committed fixtures, never data/raw.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.canara_robeco import CanaraRobecoAdapter
from mfs.ingest.managers.hdfc import HdfcAdapter
from mfs.ingest.managers.iti import ItiAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "factsheets"

# (adapter factory, fixture path, pinned scheme name, pinned PTR fraction)
# Pinned values mirror the local_data suites:
#   - HDFC Flexi Cap: 'Equity Turnover 9.14%' -> 0.0914 (percent->fraction;
#     the strongest x100 guard in the repo).
#   - Canara Robeco Large Cap: 'Portfolio Turnover Ratio 0.17 times' -> 0.17
#     ('times' convention, stored as-is).
#   - ITI Multi Cap: 'Portfolio Turnover Ratio 1.08' -> 1.08 (fraction > 1
#     is legitimate: 108% turnover).
CASES = [
    pytest.param(
        HdfcAdapter,
        FIXTURES / "hdfc" / "2026-04_extract.pdf",
        "HDFC Flexi Cap Fund",
        0.0914,
        id="hdfc-flexi-cap",
    ),
    pytest.param(
        CanaraRobecoAdapter,
        FIXTURES / "canara_robeco" / "2026-04_extract.pdf",
        "CANARA ROBECO LARGE CAP FUND",
        0.17,
        id="canara-robeco-large-cap",
    ),
    pytest.param(
        ItiAdapter,
        FIXTURES / "iti" / "2026-04_extract.pdf",
        "ITI Multi Cap Fund",
        1.08,
        id="iti-multi-cap",
    ),
]


@pytest.mark.parametrize(("adapter_cls", "pdf", "scheme", "expected_ptr"), CASES)
def test_fixture_extract_exists(adapter_cls, pdf, scheme, expected_ptr):
    """The extracts are committed artifacts — a missing file is a repo
    regression, never a skip."""
    assert pdf.exists(), f"committed fixture missing: {pdf}"


@pytest.mark.parametrize(("adapter_cls", "pdf", "scheme", "expected_ptr"), CASES)
def test_parse_ptr_pins_known_value_on_extract(adapter_cls, pdf, scheme, expected_ptr):
    adapter = adapter_cls()
    records = list(adapter.parse_ptr(pdf, "2026-04"))
    by_name = {r.scheme_name_printed: r for r in records}
    assert scheme in by_name, (
        f"{adapter_cls.__name__} found {sorted(by_name)} on the extract; "
        f"expected {scheme!r}"
    )
    rec = by_name[scheme]
    assert rec.ptr == pytest.approx(expected_ptr, abs=1e-6)
    assert rec.source_amc == adapter_cls.amc_slug


@pytest.mark.parametrize(("adapter_cls", "pdf", "scheme", "expected_ptr"), CASES)
def test_parse_ptr_extract_yields_no_garbage(adapter_cls, pdf, scheme, expected_ptr):
    """A single-page extract contains exactly one scheme page — the
    adapter must not hallucinate extra records or out-of-range values."""
    records = list(adapter_cls().parse_ptr(pdf, "2026-04"))
    assert len(records) == 1, [r.scheme_name_printed for r in records]
    assert 0 < records[0].ptr < 10
