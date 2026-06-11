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

from mfs.ingest.fbil_tbill import _parse_tbill_html


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
