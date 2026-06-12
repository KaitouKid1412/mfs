"""Tests for the shared ingest helpers (F-4, mfs.ingest._common).

parse_ptr_pages is exercised against a stubbed pdfplumber object (no PDF
rendering): the contract under test is the canonical page loop — scheme-page
gating, sentinel skip, the NaN/zero/negative PTR guard, and the
page-fallback hook. publish_ym is pinned on the January/December rollovers
that the per-adapter ``_publish_ym`` copies kept re-implementing.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from mfs.ingest import _common

# ---------------------------------------------------------------------------
# publish_ym / month tables
# ---------------------------------------------------------------------------

def test_publish_ym_default_offset_is_next_month():
    assert _common.publish_ym("2026-04") == "2026-05"


def test_publish_ym_december_rolls_year_forward():
    assert _common.publish_ym("2026-12") == "2027-01"


def test_publish_ym_january_rolls_year_backward():
    assert _common.publish_ym("2026-01", offset_months=-1) == "2025-12"


def test_publish_ym_multi_month_offset():
    assert _common.publish_ym("2026-11", offset_months=3) == "2027-02"
    assert _common.publish_ym("2026-02", offset_months=-3) == "2025-11"


def test_month_tables_are_consistent():
    assert _common.month_name("2026-04") == "April"
    assert _common.MONTH_NUMBER["april"] == 4
    assert _common.MONTH_NUMBER["apr"] == 4
    assert _common.MONTH_NUMBER["december"] == 12
    assert len(_common.MONTH_NAMES) == 12


# ---------------------------------------------------------------------------
# parse_ptr_pages — stubbed pdfplumber
# ---------------------------------------------------------------------------

class _FakePage:
    def __init__(self, text: str):
        self._text = text

    def extract_text(self):
        return self._text


class _FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_pdf(monkeypatch, texts: list[str | None]):
    pages = [_FakePage(t) for t in texts]
    monkeypatch.setattr(
        _common.pdfplumber, "open", lambda path: _FakePdf(pages)
    )


def _name_fn(text: str):
    """Scheme pages start with 'FUND:<name>'; anything else is non-scheme."""
    if text and text.startswith("FUND:"):
        return text.splitlines()[0].removeprefix("FUND:")
    return None


_PTR_RE = re.compile(r"PTR=(\S+)")


def _ptr_fn(text: str):
    m = _PTR_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _run(monkeypatch, texts, **kwargs):
    _stub_pdf(monkeypatch, texts)
    kwargs.setdefault("scheme_name_fn", _name_fn)
    kwargs.setdefault("ptr_extract_fn", _ptr_fn)
    kwargs.setdefault("amc_slug", "testamc")
    return list(_common.parse_ptr_pages(Path("fake.pdf"), **kwargs))


def test_yields_record_per_scheme_page(monkeypatch):
    recs = _run(monkeypatch, [
        "Cover page, no fund here",
        "FUND:Alpha Fund\nPTR=0.17",
        "FUND:Beta Fund\nPTR=1.08",
    ])
    assert [(r.scheme_name_printed, r.ptr) for r in recs] == [
        ("Alpha Fund", 0.17), ("Beta Fund", 1.08),
    ]
    assert all(r.source_amc == "testamc" for r in recs)


def test_non_scheme_and_empty_pages_skipped(monkeypatch):
    recs = _run(monkeypatch, ["", "TOC", "FUND:A\nPTR=0.5"])
    assert len(recs) == 1


def test_rejects_zero_negative_and_nan_ptr(monkeypatch):
    recs = _run(monkeypatch, [
        "FUND:Zero\nPTR=0",
        "FUND:Zero2\nPTR=0.0",
        "FUND:Neg\nPTR=-0.3",
        "FUND:NaN\nPTR=nan",
        "FUND:Good\nPTR=0.25",
    ])
    assert [(r.scheme_name_printed, r.ptr) for r in recs] == [("Good", 0.25)]
    assert not math.isnan(recs[0].ptr)


def test_missing_ptr_skips_page(monkeypatch):
    recs = _run(monkeypatch, ["FUND:NoPtr\nnothing here", "FUND:B\nPTR=0.4"])
    assert [r.scheme_name_printed for r in recs] == ["B"]


def test_sentinel_skip_page_re(monkeypatch):
    """A sentinel page (e.g. 'PTR not provided — scheme < 1y old') is dropped
    even when the value regex would also match something on the page."""
    sentinel = re.compile(r"Portfolio\s+Turnover\s+Ratio\s+(?:NA|-)\b")
    recs = _run(
        monkeypatch,
        [
            "FUND:NewLaunch\nPortfolio Turnover Ratio NA\nPTR=9.99",
            "FUND:Seasoned\nPTR=0.8",
        ],
        skip_page_re=sentinel,
    )
    assert [r.scheme_name_printed for r in recs] == ["Seasoned"]


def test_page_fallback_consulted_only_on_text_miss(monkeypatch):
    """page_fallback_fn fires only when ptr_extract_fn returns None, and its
    result passes through the same NaN/non-positive guard."""
    calls = []

    def fallback(page, text):
        calls.append(text.splitlines()[0])
        if "Bleed" in text:
            return 0.0914
        return -1.0  # guard must reject this

    recs = _run(
        monkeypatch,
        [
            "FUND:Clean\nPTR=0.5",          # text hit: fallback NOT consulted
            "FUND:Bleed\ncolumn-bled line",  # text miss: fallback supplies value
            "FUND:Bad\nno value",            # fallback returns -1 -> rejected
        ],
        page_fallback_fn=fallback,
    )
    assert [(r.scheme_name_printed, r.ptr) for r in recs] == [
        ("Clean", 0.5), ("Bleed", 0.0914),
    ]
    assert calls == ["FUND:Bleed", "FUND:Bad"]


def test_silence_pdfminer_sets_logger_level():
    import logging

    logging.getLogger("pdfminer").setLevel(logging.WARNING)
    _common.silence_pdfminer()
    assert logging.getLogger("pdfminer").level == logging.ERROR
