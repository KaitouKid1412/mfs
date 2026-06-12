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
whitespace, remove punctuation, split compound cap-tokens like MULTICAP ->
MULTI CAP and glued index numbers like NIFTY500 -> NIFTY 500) and use
rapidfuzz token_set_ratio to score.
A threshold-based rejection prevents silent mismatches when the AMC adds a
fund the master doesn't have yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import polars as pl
from rapidfuzz import fuzz, process

from mfs.utils.logging import get_logger

log = get_logger(__name__)

# Default acceptance threshold. token_set_ratio is generous; <85 is suspicious.
DEFAULT_THRESHOLD = 85

# Discriminator tokens that mark structurally different products (index /
# factor / passive / FoF / ETF variants of an active sibling). A printed name
# carrying one of these tokens must never fuzzy-match a candidate key that
# lacks it: token_set_ratio scores subset names at 100, so "Multi Factor
# Passive Fund of Funds" would otherwise poach onto "Multi Cap Fund", and
# "Nifty Next 50" onto "Nifty 50" (the plain-ratio guards can't catch the
# latter — the strings differ by one short token).
DISCRIMINATOR_TOKENS = frozenset({"FOF", "PASSIVE", "FACTOR", "ETF", "INDEX", "NEXT"})

# Minimum plain-ratio lead the best candidate must hold over the runner-up
# when several candidates tie on token_set_ratio. Below this the pick would
# hinge on noise — ambiguous, so we skip rather than guess (fail-fast).
_MIN_RATIO_MARGIN = 5

# token_set_ratio == 100 with a plain ratio below this floor (and no exact
# canonical equality) means one name's tokens are a strict subset of the
# other's — a sibling poach (e.g. "X Mid Cap Fund" onto "X LARGE AND MID CAP
# FUND"), not a genuine match.
_SUBSET_POACH_RATIO_FLOOR = 80

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

# Indian-MF compound cap-tokens, split into their spaced canonical form.
# AMCs and AMFI disagree freely on "Multicap" vs "Multi Cap"; token_set_ratio
# treats MULTICAP and MULTI/CAP as unrelated tokens, so the compound spelling
# defeats fuzzy matching ("Motilal Oswal Multicap Fund" scored 92.3 against
# the MIDCAP sibling instead of 100 against "MULTI CAP FUND"). Applied to
# BOTH printed names and candidate keys inside canonicalize(), so exact
# canonical equality is preserved whichever spelling each side uses.
# Variants sourced by scanning distinct scheme_master names (May 2026):
# MIDCAP(199) SMALLCAP(78) MULTICAP(74) FLEXICAP(41) LARGEMIDCAP(20)
# LARGECAP(18) BLUECHIP(17) MIDSMALL(15) MIDSMALLCAP(9) MICROCAP(2).
_COMPOUND_TOKEN_SPLITS = {
    "LARGEMIDCAP": "LARGE MID CAP",
    "MIDSMALLCAP": "MID SMALL CAP",
    "MIDSMALL": "MID SMALL",
    "MULTICAP": "MULTI CAP",
    "MIDCAP": "MID CAP",
    "SMALLCAP": "SMALL CAP",
    "LARGECAP": "LARGE CAP",
    "FLEXICAP": "FLEXI CAP",
    "MICROCAP": "MICRO CAP",
    "BLUECHIP": "BLUE CHIP",
}
# Lookarounds instead of \b: digits are word chars, and index names glue
# digits onto the compound ("LargeMidcap250", "Midsmallcap400"), which would
# defeat a trailing \b. Letter-only context guards keep e.g. MIDCAP from
# matching inside LARGEMIDCAP (handled by its own longer-first entry).
_COMPOUND_TOKEN_RE = re.compile(
    r"(?<![A-Z])(" + "|".join(_COMPOUND_TOKEN_SPLITS) + r")(?![A-Z])"
)
# Split a digit glued onto a preceding word: NIFTY500 -> NIFTY 500. Scheme
# names mix "Nifty 500" and "NIFTY500" (42x NIFTY200, 38x NIFTY500, 18x
# NIFTY50, 14x NIFTY100 in scheme_master); splitting makes both spellings
# canonicalize identically and exposes the number to the numeric-token guard.
# Digit->letter is deliberately NOT split: FMP duration codes ("1126D",
# "10JUL", "10Y") are single meaningful tokens.
_LETTER_DIGIT_SPLIT_RE = re.compile(r"(?<=[A-Z])(?=\d)")

# FOF <-> "FUND OF FUNDS"/"FUND OF FUND" equivalence for the discriminator
# check. AMCs print "FoF" where AMFI spells out "Fund of Funds" (or the
# singular "Fund of Fund" — 173 occurrences); a literal token comparison
# false-rejects e.g. "... Flexicap Passive FOF" against "... FLEXICAP
# PASSIVE FUND OF FUNDS DIRECT". Folded only for the discriminator-token
# comparison, NOT in canonicalize(), to keep the blast radius on scoring
# minimal.
_FOF_EQUIV_RE = re.compile(r"\bFUND OF FUNDS?\b")

# Numeric tokens for the numeric-token guard: extracted with findall so
# glued forms ("NIFTY500") and spaced forms ("NIFTY 500") compare equal.
_NUM_RE = re.compile(r"\d+")

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
    # Compound-token normalization (MULTICAP -> MULTI CAP, NIFTY500 ->
    # NIFTY 500, ...). Must run after uppercasing; applied to printed names
    # and candidate keys alike so both spellings land on one canonical key.
    s = _COMPOUND_TOKEN_RE.sub(lambda m: _COMPOUND_TOKEN_SPLITS[m.group(1)], s)
    s = _LETTER_DIGIT_SPLIT_RE.sub(" ", s)
    return s


@dataclass
class MatchResult:
    """One fuzzy-match attempt result.

    ``ambiguous=True`` means a candidate cleared the score threshold but the
    match was rejected as unsafe (no unique best within margin, subset-name
    poach, or a missing discriminator token) — the caller must skip the
    scheme, never guess.
    """

    printed_name: str
    matched_scheme_code: str | None
    matched_scheme_name: str | None
    score: float
    ambiguous: bool = False


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

    Exact canonical equality always wins immediately. Otherwise the
    ``token_set_ratio`` top candidates (forgiving of word order and casing)
    are re-ranked by plain ``ratio`` (Levenshtein, penalizes length
    differences), and the winner must be UNIQUE-WITH-MARGIN: clear the
    runner-up's plain ratio by >= ``_MIN_RATIO_MARGIN`` points. Any of the
    following rejects the match as ambiguous (``MatchResult.ambiguous=True``,
    logged at WARNING so the skip is loud — fail-fast, no guessing):

      - no unique best within the plain-ratio margin;
      - token_set_ratio == 100 with plain ratio < ``_SUBSET_POACH_RATIO_FLOOR``
        (the smaller token set is a subset of the larger — e.g. "Bank of
        India MID CAP FUND" against "Bank of India LARGE AND MID CAP FUND");
      - the NUMERIC tokens of printed name and matched key differ as sets,
        regardless of score (e.g. "Nifty 500 Index Fund" scored 98.5 against
        "Nifty 50 Index Fund" — one digit of edit distance, but a different
        product; equal sets like {150, 50} on both sides pass);
      - the printed name carries a ``DISCRIMINATOR_TOKENS`` member the
        matched key lacks (e.g. "Multi Factor Passive Fund of Funds" must
        not resolve to "Multi Cap Fund", "Nifty Next 50" not to "Nifty 50").
        "FUND OF FUNDS"/"FUND OF FUND" is folded to FOF on both sides first,
        so "... Passive FOF" is not false-rejected against its own
        "... PASSIVE FUND OF FUNDS" master row.
    """
    canon_in = canonicalize(printed_name)
    if not canon_in or not candidates:
        return MatchResult(printed_name, None, None, 0.0)
    # Exact canonical equality wins immediately — a literal name match can
    # never be ambiguous, and adapter-level disambiguation rewrites (kotak,
    # dsp, motilal_oswal) rely on this to land on their intended sibling.
    exact = candidates.get(canon_in)
    if exact is not None:
        return MatchResult(
            printed_name, exact["scheme_code"], exact["scheme_name"], 100.0
        )
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
    # Contenders: candidates within 2 points of top_score, re-ranked by
    # plain ratio (Levenshtein on the full strings), which rewards equal
    # length over subset-of-longer-name candidates.
    contenders = sorted(
        (
            (k, s, fuzz.ratio(canon_in, k))
            for k, s, _ in results
            if s >= top_score - 2
        ),
        key=lambda t: (t[2], t[1]),
        reverse=True,
    )
    best_key, best_score, best_ratio = contenders[0]

    def _ambiguous(reason: str) -> MatchResult:
        log.warning(
            "scheme_match.ambiguous",
            printed=printed_name,
            reason=reason,
            top_candidates=[
                {"key": k, "token_set": float(s), "ratio": round(r, 1)}
                for k, s, r in contenders
            ],
        )
        return MatchResult(printed_name, None, None, float(top_score), ambiguous=True)

    if (
        len(contenders) > 1
        and best_ratio - contenders[1][2] < _MIN_RATIO_MARGIN
    ):
        return _ambiguous("no_unique_best_within_margin")
    if best_score == 100 and best_ratio < _SUBSET_POACH_RATIO_FLOOR:
        return _ambiguous("subset_name_poach")
    # Numeric-token guard: differing number sets mean a different product
    # (Nifty 500 vs Nifty 50, 200 vs 100), however close the edit distance.
    nums_in = {int(n) for n in _NUM_RE.findall(canon_in)}
    nums_key = {int(n) for n in _NUM_RE.findall(best_key)}
    if nums_in != nums_key:
        return _ambiguous(
            f"numeric_tokens_mismatch:printed={sorted(nums_in)},"
            f"candidate={sorted(nums_key)}"
        )
    # Discriminator comparison with FOF <-> FUND OF FUND(S) folded so the
    # spelled-out master name isn't false-rejected against a printed "FoF".
    folded_in = _FOF_EQUIV_RE.sub("FOF", canon_in)
    folded_key = _FOF_EQUIV_RE.sub("FOF", best_key)
    missing = (set(folded_in.split()) & DISCRIMINATOR_TOKENS) - set(folded_key.split())
    if missing:
        return _ambiguous(
            "discriminator_tokens_missing:" + ",".join(sorted(missing))
        )
    info = candidates[best_key]
    return MatchResult(
        printed_name,
        info["scheme_code"],
        info["scheme_name"],
        float(best_score),
    )
