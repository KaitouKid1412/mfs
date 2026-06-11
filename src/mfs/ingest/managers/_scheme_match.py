"""Fuzzy-match a printed scheme name from an AMC factsheet to scheme_master.

AMC factsheets typically print one row per fund (e.g. "HDFC Flexi Cap Fund"),
without explicit plan/option suffixes. Our scheme_master has multiple rows per
fund — one per (plan_type, option_type) combination — but Phase 2 ranks only
DIRECT + GROWTH, so we restrict matching to that subset.

Why this is a fuzzy match rather than exact: the AMC's printed name often
differs subtly from AMFI's recorded name. Examples seen in real data:
  - AMC says "HDFC Top 100 Fund" → AMFI: "HDFC TOP 100 FUND - DIRECT PLAN - GROWTH OPTION"
  - AMC says "HDFC Defence Fund" → AMFI: "HDFC Defence Fund - Direct Plan - Growth Option"

We canonicalize both sides (uppercase, strip plan/option suffix, collapse
whitespace, remove punctuation) and use rapidfuzz token_set_ratio to score.
A threshold-based rejection prevents silent mismatches when the AMC adds a
fund the master doesn't have yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import polars as pl
from rapidfuzz import fuzz, process

# Default acceptance threshold. token_set_ratio is generous; <85 is suspicious.
DEFAULT_THRESHOLD = 85

# Adapter slugs are short identifiers ('absl', 'nippon') but scheme_master's
# `amc_code` column is the long-form slug derived from the AMC name (e.g.
# 'aditya_birla_sun_life'). Map adapter slug → scheme_master amc_code so the
# matcher can find the right candidate set. Only AMCs whose slugs differ
# need an entry. HDFC and SBI canonicalize identically so they're omitted.
_ADAPTER_SLUG_TO_SCHEME_MASTER_AMC_CODE = {
    "absl": "aditya_birla_sun_life",
    "icici_pru": "icici_prudential",
    "kotak": "kotak_mahindra",
    "mirae": "mirae_asset",
    "nippon": "nippon_india",
    "franklin": "franklin_templeton",
    "baroda_bnp": "baroda_bnp_paribas",
}


def resolve_scheme_master_amc_code(adapter_slug: str) -> str:
    """Translate an adapter's amc_slug into the scheme_master.amc_code value.

    Adapters use short slugs ('absl'); scheme_master normalizes 'Aditya Birla
    Sun Life Mutual Fund' to 'aditya_birla_sun_life'. This mapping is the
    single source of truth for that translation, shared by managers and
    holdings ingestion packages.
    """
    return _ADAPTER_SLUG_TO_SCHEME_MASTER_AMC_CODE.get(adapter_slug, adapter_slug)

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

# "(erstwhile ...)" / "(formerly ...)" rename parenthetical — descriptive noise
# AMFI appends to a renamed scheme's name. Stripped before matching.
_RENAME_PARENS_RE = re.compile(r"\s*\((?:erstwhile|formerly)\b[^)]*\)", re.IGNORECASE)

# Strip the plan/option suffix only when it is unambiguously a SUFFIX -- i.e.
# attached to the fund-name body by a hyphen / em-dash / en-dash separator.
# This guards against fund names whose body contains the words "Growth",
# "IDCW", or "Dividend" (e.g. "Nippon India Growth Mid Cap Fund", "HDFC
# Dividend Yield Fund", "Axis Growth Opportunities Fund"), which the prior
# unanchored regex incorrectly truncated to "NIPPON INDIA" / "HDFC" / "AXIS",
# causing all schemes for those AMCs to collapse to one fuzzy-match key.
#
# We strip:
#   - " - Direct/Regular Plan ..." onward
#   - " - Growth/IDCW/Dividend [Option|Reinvestment|Payout|Plan] ..." onward
#   - " - Growth" / " - IDCW" / " - Dividend" at end of line
# The separator (hyphen / em-dash / en-dash) is REQUIRED, otherwise the
# match is rejected.
_PLAN_OPTION_SUFFIX_RE = re.compile(
    r"\s*[-—–]\s*"
    r"(?:"
    r"(?:direct|regular)\s+plan\b"
    r"|(?:growth|idcw|dividend)(?:\s+(?:option|reinvestment|payout|plan))?\b"
    r")"
    r".*$",
    re.IGNORECASE,
)

# A few AMCs print suffixes WITHOUT a separator -- e.g. "Direct Plan Growth
# Plan Growth Option" (Nippon's older naming) or "GROWTH OPTION-Direct Plan"
# (quant). Once the body has been hyphen-stripped above, any remaining
# Plan/Option keywords at the END of the name are safe to drop, because by
# then we know we're inside the suffix region (the body normally ends with
# "Fund", "ETF", "FOF", a year, or "Plan A"/"Plan D" style series tags).
_TRAILING_PLAN_OPTION_RE = re.compile(
    r"\s+(?:"
    r"(?:direct|regular)\s+plan(?:\s+(?:growth|idcw|dividend)\s+plan)?"
    r"(?:\s*-?\s*(?:growth|idcw|dividend|bonus)(?:\s+option|\s+reinvestment|\s+payout)?)?"
    r"|(?:growth|idcw|dividend)\s+plan\s*[-–—]?\s*"
    r"(?:growth|idcw|dividend|bonus)(?:\s+option|\s+reinvestment|\s+payout)?"
    r")\s*$",
    re.IGNORECASE,
)


def canonicalize(name: str) -> str:
    """Strip plan/option suffix, punctuation, collapse whitespace, uppercase.

    The suffix stripper requires a hyphen / em-dash / en-dash separator
    between the fund-name body and the Plan/Option keyword block. This
    prevents fund names that legitimately contain "Growth" / "IDCW" /
    "Dividend" (e.g. "Nippon India Growth Mid Cap Fund") from being
    truncated to a 2-token prefix that collapses every scheme in the AMC
    onto one fuzzy-match key.
    """
    if not name:
        return ""
    # Strip "(erstwhile/formerly X)" rename parentheticals. AMFI keeps the old
    # name as a parenthetical on renamed schemes (e.g. "ICICI Prudential Large
    # Cap Fund (erstwhile Bluechip Fund)"); the AMC factsheet prints only the
    # new name. Left in, the extra tokens lengthen the canonical key so the
    # Levenshtein tie-break picks a wrong shorter sibling (e.g. "Large & Mid
    # Cap Fund") over the true target. Both candidates tie at token_set_ratio
    # 100, so the tie-break decides — and the parenthetical flips it.
    s = _RENAME_PARENS_RE.sub(" ", name)
    s = _PLAN_OPTION_SUFFIX_RE.sub("", s)
    s = _TRAILING_PLAN_OPTION_RE.sub("", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip().upper()
    return s


@dataclass
class MatchResult:
    """One fuzzy-match attempt result."""

    printed_name: str
    matched_scheme_code: str | None
    matched_scheme_name: str | None
    score: float


def build_candidate_index(
    scheme_master: pl.DataFrame,
    amc_slug: str,
    plan_type: str = "DIRECT",
    option_type: str = "GROWTH",
) -> dict[str, dict]:
    """Return {canonical_name: {scheme_code, scheme_name}} for one AMC.

    `amc_slug` matches scheme_master.amc_code (lowercased). The plan/option
    filter restricts to the ranked universe so we don't get false matches to
    legacy bonus / regular plans.
    """
    if scheme_master.is_empty():
        return {}
    sm_amc_code = resolve_scheme_master_amc_code(amc_slug)
    df = scheme_master.filter(
        (pl.col("amc_code") == sm_amc_code)
        & (pl.col("plan_type") == plan_type)
        & (pl.col("option_type") == option_type)
        & (pl.col("is_active") == True)  # noqa: E712
    )
    # Drop AMFI's legacy "Bonus Option" rows. These share NAV/portfolio with
    # the Growth Option sibling but get classified under plan_type=DIRECT,
    # option_type=GROWTH in scheme_master because the name contains "Growth
    # Plan". Letting them through pollutes the candidate index with a
    # canonical-name duplicate (or, worse, shadows the real Growth row when
    # the bonus row sorts first in iteration order). rank/shortlist.py
    # already dedupes Bonus rows post-scoring; we apply the same filter
    # here so factsheet ingestion is consistent.
    df = df.filter(~pl.col("scheme_name").str.contains(r"(?i)\bBonus\b"))
    out: dict[str, dict] = {}
    for r in df.iter_rows(named=True):
        canon = canonicalize(r["scheme_name"])
        if not canon:
            continue
        # On duplicates (rare), keep the first; log via caller if needed.
        out.setdefault(canon, {
            "scheme_code": r["scheme_code"],
            "scheme_name": r["scheme_name"],
        })
    return out


def match_one(
    printed_name: str,
    candidates: dict[str, dict],
    threshold: int = DEFAULT_THRESHOLD,
) -> MatchResult:
    """Match one printed scheme name against the AMC's candidate index.

    Uses ``token_set_ratio`` as the primary scorer (forgiving of word order
    and casing differences), then breaks ties with ``ratio`` (Levenshtein)
    which penalizes length differences. The tie-breaker is critical for
    subset names — without it, "Bank of India MID CAP FUND" would tie at
    score 100 against both "Bank of India MID CAP FUND" and "Bank of India
    LARGE AND MID CAP FUND" (the smaller token set is a subset of the
    larger), and dict iteration order would decide the winner. With it,
    the exact match wins because ``ratio`` rewards equal length.
    """
    canon_in = canonicalize(printed_name)
    if not canon_in or not candidates:
        return MatchResult(printed_name, None, None, 0.0)
    keys = list(candidates.keys())
    # Get top candidates by token_set_ratio (score_cutoff for early-exit).
    results = process.extract(
        canon_in, keys, scorer=fuzz.token_set_ratio, limit=5
    )
    if not results:
        return MatchResult(printed_name, None, None, 0.0)
    top_score = results[0][1]
    if top_score < threshold:
        return MatchResult(printed_name, None, None, float(top_score))
    # Tie-break: among candidates within 2 points of top_score, pick the
    # one with the highest plain ratio (Levenshtein on the full strings).
    # This selects exact-length matches over subset-of-longer-name matches.
    tied = [(k, s) for k, s, _ in results if s >= top_score - 2]
    if len(tied) > 1:
        tied.sort(
            key=lambda ks: (fuzz.ratio(canon_in, ks[0]), ks[1]),
            reverse=True,
        )
    matched_key, score = tied[0]
    info = candidates[matched_key]
    return MatchResult(
        printed_name,
        info["scheme_code"],
        info["scheme_name"],
        float(score),
    )
