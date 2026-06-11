"""Tests for the 91-day T-bill PR parser, focused on tenor disambiguation.

RBI publishes auction-result PRs in two eras:
  - old: one PR per tenor ("91-Days / 182-Days / 364-Days Treasury Bills : Full
    Auction Result"), YTM printed with a hyphen ("YTM - 8.1022%");
  - new: one combined table titled "91-Day, 182-Day and 364-Day ... : Cut-off"
    or the tenor-less "Treasury Bills: Full Auction Result", YTM with a colon.

The parser must extract ONLY the 91-day cut-off; a 182/364/odd-tenor/MSS PR
must not have its YTM mislabeled as the 91-day rate.
"""

from __future__ import annotations

import contextlib
from datetime import date
from pathlib import Path

import httpx
import pytest

from mfs.ingest import fbil_tbill as fbil
from mfs.ingest.fbil_tbill import _parse_tbill_html
from mfs.io.http import TransientHttpError

_FIXTURES = Path(__file__).parent / "fixtures" / "rbi"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8", errors="ignore")


def _pr(title: str, body: str, d: str = "Jan 02, 2013") -> str:
    return (
        f'<td class="tableheader">Date : {d}</td>'
        f'<td class="tableheader"><b>{title}</b></td>'
        f"<table>{body}</table>"
    )


def _rate(title, body, d="Jan 02, 2013"):
    r = _parse_tbill_html(_pr(title, body, d), prid=1)
    return None if r is None else r["rate_annual_pct"]


def test_old_91day_hyphen_ytm_parsed():
    body = "IV. Cut-off price ` 98.02 (YTM - 8.1022%)"
    assert _rate("91-Days Treasury Bills : Full Auction Result", body) == 8.1022


def test_old_182day_rejected():
    body = "IV. Cut-off price ` 96.14 (YTM - 8.052%)"
    assert _rate("182-Days Treasury Bills : Full Auction Result", body) is None


def test_old_364day_rejected():
    body = "IV. Cut-off price ` 92.74 (YTM - 7.8498%)"
    assert _rate("364-Days Treasury Bills : Full Auction Result", body) is None


def test_odd_tenor_mss_rejected():
    # 329/317-day MSS bills lack 182/364 but are NOT 91-day; must be rejected.
    assert _rate("329-Days Treasury Bills : Full Auction Result",
                 "IV. Cut-off price ` 94.56 (YTM - 6.3825%)") is None
    assert _rate("317-Days MSS- Treasury Bills : Full Auction Result",
                 "IV. Cut-off price ` 94.69 (YTM - 6.4569%)") is None


def test_new_combined_cutoff_takes_91day_first():
    # Combined cut-off PR: each tenor is its own table row (tags between them), so
    # the first cut-off row (91-day) is the one read.
    body = (
        "<tr><td>Cut-off Price and Implicit Yield at Cut-Off Price</td>"
        "<td>` 98.30 (YTM: 6.9512%)</td></tr>"  # 91d
        "<tr><td>Cut-off Price and Implicit Yield at Cut-Off Price</td>"
        "<td>` 96.60 (YTM: 7.0500%)</td></tr>"  # 182d
    )
    assert _rate("91-Day, 182-Day and 364-Day T-Bill Auction Result: Cut-off", body) == 6.9512


def test_tenorless_full_auction_accepted():
    body = "IV. Cut-off price ` 98.40 (YTM : 6.5300%)"
    assert _rate("Treasury Bills: Full Auction Result", body) == 6.53


def test_degenerate_zero_ytm_unmatched():
    # No-issuance week prints "₹ 0.00 (YTM : 0 %)" — integer 0, no decimal -> skip.
    body = "IV. Cut-off price ` 0.00 (YTM : 0 %)"
    assert _rate("91-Days Treasury Bills : Full Auction Result", body) is None


def test_non_tbill_page_rejected():
    assert _rate("India's External Debt as at the end of June 2020",
                 "some unrelated content") is None


# ---------------------------------------------------------------------------
# Shell-page detection (Urgent-1): RBI serves ~120KB 200-OK full-chrome shell
# pages for nonexistent prids. A shell must be treated as a transient fetch
# failure — never cached, never read as "prid absent" — and a shell already in
# the cache must be invalidated on read.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _fake_client():
    yield object()


def _patch_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fbil.paths,
        "rbi_press_release_raw",
        lambda prid: tmp_path / f"prid_{prid}.html",
    )


def test_shell_page_not_real_pr():
    html = _fixture("shell_page.html")
    # The two false-positive traps the fixture preserves: shell chrome matches
    # _DATE_RE (lowercase 'date: Jun 08, 2026' + IGNORECASE) and contains the
    # bare string 'tableheader' inside JavaScript. Detection must therefore
    # anchor on the attribute form class="tableheader", which shells lack.
    assert fbil._DATE_RE.search(html) is not None
    assert "tableheader" in html
    assert fbil._is_real_pr_page(html) is False
    assert fbil._parse_tbill_html(html, 63000) is None


def test_real_cutoff_fixture_parses():
    html = _fixture("cutoff_91d_2026-06-03.html")
    assert fbil._is_real_pr_page(html) is True
    assert fbil._parse_tbill_html(html, 62855) == {
        "prid": 62855,
        "auction_date": date(2026, 6, 3),
        "rate_annual_pct": 5.5586,
    }


def test_fetch_shell_raises_transient():
    # Undecorated path (__wrapped__) so tenacity's 1-8s backoff is skipped.
    shell_html = _fixture("shell_page.html")
    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=shell_html))
    )
    with pytest.raises(TransientHttpError, match="WAF/shell page"):
        fbil._fetch_prid_with_retry.__wrapped__(client, 99999)


def test_shell_not_cached_and_counted_failed(monkeypatch, tmp_path):
    _patch_cache_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(fbil, "_rbi_client", _fake_client)
    monkeypatch.setattr(fbil.time, "sleep", lambda *a, **k: None)

    def _raise_shell(client, prid):
        raise TransientHttpError("shell")

    monkeypatch.setattr(fbil, "_fetch_prid_with_retry", _raise_shell)

    # Transient failure surfaces as (None, reason) and the cache write is
    # never reached — no shell file may land on disk.
    assert fbil._fetch_prid_html(object(), 5) == (None, "shell")
    assert not (tmp_path / "prid_5.html").exists()
    # The walk counts the shell toward the failure cap, not as a hit/absence.
    assert fbil._walk_prid_range(5, 5) == ([], 1)
    assert not (tmp_path / "prid_5.html").exists()


def test_cached_shell_invalidated(monkeypatch, tmp_path):
    _patch_cache_dir(monkeypatch, tmp_path)
    cache = tmp_path / "prid_5.html"
    cache.write_text(_fixture("shell_page.html"), encoding="utf-8")
    real_html = _fixture("cutoff_91d_2026-06-03.html")
    monkeypatch.setattr(fbil, "_fetch_prid_with_retry", lambda client, prid: real_html)

    # A cached shell must be ignored (re-fetched) and overwritten on success.
    html, failure = fbil._fetch_prid_html(object(), 5)
    assert (html, failure) == (real_html, None)
    assert 'class="tableheader"' in cache.read_text(encoding="utf-8")
