"""Build the canonical scheme dimension from the latest AMFI NAVAll + nav_daily history.

For each (scheme_code) we derive:
- plan_type: DIRECT | REGULAR | UNKNOWN
- option_type: GROWTH | IDCW | UNKNOWN
- amc_code: a normalized slug of amc_name
- canonical_category: human category (Large Cap, Mid Cap, …)
- benchmark_ticker: from configs/benchmarks.csv
- base_fund_id: shared key across Direct/Regular and Growth/IDCW siblings
- inception_date: first NAV date in history
- is_active: present in today's AMFI listing
- last_seen_date: max NAV date observed

Persistence is a diff-sync, not a rebuild (survivorship containment): schemes
absent from today's listing are KEPT with is_active=false and a departed_at
stamp, never deleted — see ``writers.sync_scheme_master``.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

import polars as pl

from mfs.db import queries as q
from mfs.db import writers as w
from mfs.errors import IngestError, PipelineError
from mfs.ingest import amfi_nav
from mfs.master.benchmark_map import benchmark_for
from mfs.utils.logging import get_logger

log = get_logger(__name__)

# Map AMFI category strings to our canonical names. Match by substring (case-insensitive).
# "Sector" / "Thematic" categories are deliberately mapped to the placeholder
# "Sectoral/Thematic" which is then resolved to a specific sector via SECTOR_RULES
# below, using the scheme NAME (since AMFI puts the sector detail there, not in
# its own category field).
CATEGORY_RULES: list[tuple[str, str]] = [
    ("Large Cap", "Large Cap"),
    ("Large & Mid", "Large & Mid Cap"),
    ("Large and Mid", "Large & Mid Cap"),
    ("Mid Cap", "Mid Cap"),
    ("Small Cap", "Small Cap"),
    ("Multi Cap", "Multi Cap"),
    ("Flexi Cap", "Flexi Cap"),
    ("Focused", "Focused"),
    ("ELSS", "ELSS"),
    ("Equity Linked Savings", "ELSS"),
    ("Tax Saver", "ELSS"),
    ("Value", "Value"),
    ("Contra", "Contra"),
    ("Dividend Yield", "Dividend Yield"),
    ("Aggressive Hybrid", "Aggressive Hybrid"),
    ("Balanced Advantage", "Balanced Advantage"),
    ("Dynamic Asset Allocation", "Balanced Advantage"),
    ("Equity Savings", "Equity Savings"),
    ("Sector", "Sectoral/Thematic"),
    ("Thematic", "Sectoral/Thematic"),
]

# Sector classification rules — applied AFTER CATEGORY_RULES resolves a scheme to
# the catchall "Sectoral/Thematic". Order matters: more specific tokens come
# first (e.g., FMCG before Consumption, Healthcare before Pharma to keep
# combined-sector funds in the broader bucket).
SECTOR_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bESG\b|responsible", re.IGNORECASE), "ESG"),
    (re.compile(r"\bMNC\b|multinational", re.IGNORECASE), "MNC"),
    (re.compile(r"\bFMCG\b", re.IGNORECASE), "FMCG"),
    (re.compile(r"\bbank(ing)?\b|financial services|finserv|fin\.?\s*serv", re.IGNORECASE),
     "Banking & Financial Services"),
    (re.compile(r"\b(IT|Tech(nology)?|Digital|Software)\b", re.IGNORECASE), "IT"),
    (re.compile(r"pharma|health\s*care|health|biotech", re.IGNORECASE),
     "Pharma & Healthcare"),
    (re.compile(r"\bauto(mobile)?\b|transport(ation)?|mobility", re.IGNORECASE), "Auto"),
    (re.compile(r"energy|power|oil\s*&?\s*gas|natural\s*resources", re.IGNORECASE), "Energy"),
    (re.compile(r"infra(structure)?", re.IGNORECASE), "Infrastructure"),
    (re.compile(r"\bPSU\b|public\s+sector|PSE\b|PSU Equity", re.IGNORECASE), "PSU"),
    (re.compile(r"manufactur", re.IGNORECASE), "Manufacturing"),
    (re.compile(r"consum(ption|er)", re.IGNORECASE), "Consumption"),
]


def _strip_amc_name(scheme_name: str, amc_name: str | None) -> str:
    """Remove the AMC's house name from a scheme name so sector classification
    keys off the fund's MANDATE, not the house name.

    Without this, an AMC whose name contains a sector token mis-classifies its
    own sector/thematic funds — e.g. "BANK OF INDIA Manufacturing &
    Infrastructure Fund" matches the Banking rule via "BANK", and "Bajaj Finserv
    Healthcare Fund" matches it via "Finserv". Stripping the leading AMC name
    leaves "Manufacturing & Infrastructure Fund" / "Healthcare Fund" to classify.
    """
    if not amc_name:
        return scheme_name
    amc = re.sub(r"\bmutual fund\b", "", amc_name, flags=re.IGNORECASE).strip()
    if not amc:
        return scheme_name
    return re.sub(r"^\s*" + re.escape(amc) + r"\b", "", scheme_name, flags=re.IGNORECASE).strip()


def _classify_sector(scheme_name: str) -> str:
    """Map a Sectoral/Thematic scheme to a specific sector bucket using its name.

    Returns one of the 12 sector buckets, or 'Thematic' as the residual. Pass an
    AMC-name-stripped scheme name (see ``_strip_amc_name``) so the house name
    can't trigger a sector rule.
    """
    if not scheme_name:
        return "Thematic"
    for pat, sector in SECTOR_RULES:
        if pat.search(scheme_name):
            return sector
    return "Thematic"


def _amc_slug(s: str) -> str:
    s = re.sub(r"\bMutual Fund\b", "", s, flags=re.IGNORECASE).strip()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# Option tokens enumerated empirically from the AMFI NAVAll snapshot
# (2026-06-11). IDCW appears as the acronym, the spelled-out phrase
# ("... Income Distribution cum Capital Withdrawal ..."), bare "Dividend",
# the abbreviation "Div" ("Quarterly Div Option"), and recurring AMFI typos
# (IDWC / ICDW / Divdend). ICICI Pru names its growth plans "Cumulative
# Option". "Bonus" marks legacy bonus-unit plans — neither growth nor IDCW —
# which stay UNKNOWN (out of the ranked universe).
_IDCW_PATTERN = (
    r"idcw|idwc|icdw|dividend|divdend|\bdiv\b"
    r"|income\s+distribution|capital\s+withdrawal"
)
_IDCW_RE = re.compile(_IDCW_PATTERN, re.IGNORECASE)

# "Dividend Yield" is a fund MANDATE, not an option token: without stripping
# it first, every growth plan in the Dividend Yield category trips the
# 'dividend' token and is misclassified IDCW (the whole category was silently
# excluded from the ranked universe). Polars' rust regex has no lookahead, so
# both call sites strip-then-match instead of using (?!\s+yield).
_DIVIDEND_YIELD_PATTERN = r"dividend[\s-]*yield"
_DIVIDEND_YIELD_RE = re.compile(_DIVIDEND_YIELD_PATTERN, re.IGNORECASE)

# Every token that marks an explicit option in a scheme name. A name carrying
# NONE of these is a candidate for the single-option GROWTH default below.
# (?i) inline flag so the same pattern works in polars' rust-regex engine.
_ANY_OPTION_TOKEN_PATTERN = (
    rf"(?i){_IDCW_PATTERN}|growth|cumulative|bonus|payout|reinvest"
)


def _classify_plan_option(scheme_name: str) -> tuple[str, str]:
    name_lc = scheme_name.lower()
    plan = "UNKNOWN"
    if re.search(r"\bdirect( plan)?\b", name_lc):
        plan = "DIRECT"
    elif re.search(r"\bregular( plan)?\b", name_lc):
        plan = "REGULAR"
    elif "direct" in name_lc:
        plan = "DIRECT"
    elif "regular" in name_lc:
        plan = "REGULAR"
    option = "UNKNOWN"
    opt_name = _DIVIDEND_YIELD_RE.sub("", name_lc)
    if _IDCW_RE.search(opt_name):
        option = "IDCW"
    elif "growth" in opt_name:
        option = "GROWTH"
    elif "cumulative" in opt_name:
        # ICICI Pru growth plans are named "Cumulative Option". The IDCW
        # branch wins first so "Cumulative IDCW" oddities keep IDCW.
        option = "GROWTH"
    return plan, option


def _apply_single_option_default(df: pl.DataFrame) -> pl.DataFrame:
    """Default token-less UNKNOWN options to GROWTH for single-option funds.

    Some AMCs list their sole plan with no option token at all (e.g.
    "Samco Mid Cap Fund - Direct Plan" — the growth plan, and the only one).
    Within a (base_fund_id, plan_type) group: a row whose option is UNKNOWN,
    whose name carries no option token, and that has no sibling with a
    resolved option IS the fund's single (growth) option. A resolved
    GROWTH/IDCW sibling means genuine ambiguity — keep UNKNOWN.
    """
    has_resolved_sibling = (
        (pl.col("option_type") != "UNKNOWN").any().over("base_fund_id", "plan_type")
    )
    no_option_token = ~(
        pl.col("scheme_name")
        .str.replace_all(f"(?i){_DIVIDEND_YIELD_PATTERN}", "")
        .str.contains(_ANY_OPTION_TOKEN_PATTERN)
    )
    return df.with_columns(
        pl.when(
            (pl.col("option_type") == "UNKNOWN")
            & no_option_token
            & ~has_resolved_sibling
        )
        .then(pl.lit("GROWTH"))
        .otherwise(pl.col("option_type"))
        .alias("option_type")
    )


def _direct_unknown_rankable(df: pl.DataFrame) -> pl.DataFrame:
    """Active DIRECT schemes in a rankable category still carrying UNKNOWN
    option — each is silently excluded from the ranked universe, so this
    listing is the recurring tripwire for AMFI option-naming drift."""
    return df.filter(
        pl.col("is_active")
        & (pl.col("plan_type") == "DIRECT")
        & (pl.col("option_type") == "UNKNOWN")
        & pl.col("canonical_category").is_not_null()
    ).select("scheme_code", "scheme_name")


# Closed-ended and interval schemes are excluded from the equity universe: they
# are not open for purchase and their NAV history is finite. AMFI prefixes every
# category with the scheme type, e.g. "Close Ended Schemes(ELSS)" or
# "Interval Fund Schemes(...)". Without this guard, closed-ended tax-saver series
# (SBI/UTI/Sundaram/Bank of India "Long Term Advantage" etc.) leak into the
# ranked universe via the "ELSS"/"Tax Saver" substring rules below.
_CLOSED_OR_INTERVAL_RE = re.compile(r"closed?[\s-]*end(?:ed)?|interval", re.IGNORECASE)


def _canonical_category(raw: str | None) -> str | None:
    if not raw:
        return None
    if _CLOSED_OR_INTERVAL_RE.search(raw):
        return None
    for needle, canon in CATEGORY_RULES:
        if needle.lower() in raw.lower():
            return canon
    return None


# F-12 category-relabel guard: _canonical_category returns None silently for
# any unmatched AMFI category string, so an AMFI/SEBI relabel (e.g.
# "Flexi Cap Fund" -> "Flexicap Fund") would silently evaporate a whole
# canonical category from the ranked universe. The guard halts build() when a
# category that currently holds at least this many ACTIVE schemes in the DB
# would drop to zero matches in the new snapshot — halt-and-fix beats ranking
# a silently shrunken universe (fail-fast invariant). A legitimate category
# retirement requires updating CATEGORY_RULES, which is the intended response.
CATEGORY_GUARD_MIN_SCHEMES = 5


def _existing_active_category_counts() -> dict[str, int]:
    """Active-scheme count per canonical_category currently in the DB."""
    from mfs.db.connection import connect

    with connect() as c:
        rows = c.execute(
            "SELECT canonical_category, COUNT(*) FROM scheme_master "
            "WHERE is_active AND canonical_category IS NOT NULL "
            "GROUP BY canonical_category"
        ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def check_category_disappearance(
    new_counts: dict[str, int],
    existing_counts: dict[str, int],
    unmatched_raw: dict[str, int],
    threshold: int = CATEGORY_GUARD_MIN_SCHEMES,
) -> None:
    """Raise PipelineError if any currently-populated category (>= threshold
    active schemes in the DB) has zero matches in the new build.

    ``unmatched_raw`` (raw AMFI category string -> scheme count from the new
    snapshot) is included in the error so the operator can see what the
    relabeled strings look like and patch CATEGORY_RULES.
    """
    vanished = sorted(
        cat
        for cat, n in existing_counts.items()
        if n >= threshold and new_counts.get(cat, 0) == 0
    )
    if not vanished:
        return
    unmatched_desc = (
        "; ".join(
            f"{raw!r} ({n} schemes)"
            for raw, n in sorted(unmatched_raw.items(), key=lambda kv: -kv[1])
        )
        or "<none — categories may have been re-mapped, not just relabeled>"
    )
    raise PipelineError(
        "scheme_master category-relabel guard: categories "
        f"{vanished} hold >= {threshold} active schemes in "
        "the DB but matched 0 schemes in the new AMFI snapshot. A SEBI/AMFI "
        "category relabel would otherwise silently evaporate them from the "
        "ranked universe. Unmatched raw AMFI category strings in this "
        f"snapshot: {unmatched_desc}. Fix: extend CATEGORY_RULES in "
        "src/mfs/master/scheme_master.py (or confirm a genuine category "
        "retirement) and re-run `mfs build scheme-master`."
    )


def _base_fund_id(scheme_name: str, amc_slug: str) -> str:
    """Strip plan/option tokens to derive a shared id across siblings."""
    s = scheme_name
    s = re.sub(r"\b(direct|regular)( plan)?\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(growth|idcw|dividend|reinvestment|payout)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(plan|option)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return f"{amc_slug}::{re.sub(r'[^a-z0-9]+', '_', s).strip('_')}"


def build() -> pl.DataFrame:
    """Pull today's AMFI snapshot, derive scheme master rows, diff-sync into
    Postgres (departed schemes are kept with is_active=false, never dropped)."""
    today = date.today()
    content = amfi_nav.fetch_today().decode("utf-8", errors="replace")
    snap = amfi_nav.parse_to_dataframe(content)
    if snap.is_empty():
        raise IngestError("Empty AMFI NAVAll snapshot; cannot build scheme master")

    # Derive plan/option/category/amc_slug/base_fund_id per row
    snap_pd = snap.unique("scheme_code", keep="last")
    rows = []
    # F-12: raw AMFI category strings that matched NO rule (and are not the
    # deliberately-excluded closed/interval types) — relabel tripwire input.
    unmatched_raw: Counter[str] = Counter()
    for r in snap_pd.iter_rows(named=True):
        scheme_name = r["scheme_name"] or ""
        amc_name = r["amc_name"] or "Unknown"
        plan, option = _classify_plan_option(scheme_name)
        raw_category = r["amfi_category"]
        canon = _canonical_category(raw_category)
        if (
            canon is None
            and raw_category
            and not _CLOSED_OR_INTERVAL_RE.search(raw_category)
        ):
            unmatched_raw[raw_category] += 1
        # Sub-classify sectoral/thematic funds based on scheme name — with the
        # AMC house name stripped so it can't trigger a sector rule (e.g.
        # "BANK OF INDIA ..." / "Bajaj Finserv ...").
        if canon == "Sectoral/Thematic":
            canon = _classify_sector(_strip_amc_name(scheme_name, amc_name))
        amc_code = _amc_slug(amc_name)
        rows.append(
            {
                "scheme_code": r["scheme_code"],
                "isin_growth": r["isin_growth"],
                "isin_idcw": r["isin_idcw"],
                "scheme_name": scheme_name,
                "amc_name": amc_name,
                "amc_code": amc_code,
                "plan_type": plan,
                "option_type": option,
                "amfi_category": r["amfi_category"],
                "canonical_category": canon,
                "benchmark_ticker": benchmark_for(canon),
                "inception_date": None,
                "base_fund_id": _base_fund_id(scheme_name, amc_code),
                "is_active": True,
                "last_seen_date": today,
            }
        )

    df = pl.DataFrame(rows)
    df = _apply_single_option_default(df)

    # Compute inception_date and last_seen_date from nav_daily history (Postgres-side
    # aggregation; pulling 30M+ NAV rows into the process just to GROUP BY is wasteful).
    from mfs.db.connection import connect

    with connect() as c:
        bounds_rows = c.execute(
            "SELECT scheme_code, MIN(nav_date) AS inception_date, "
            "MAX(nav_date) AS last_seen_date_hist FROM nav_daily GROUP BY scheme_code"
        ).fetchall()
    if bounds_rows:
        bounds = pl.DataFrame(
            bounds_rows,
            schema={
                "scheme_code": pl.Utf8,
                "inception_date": pl.Date,
                "last_seen_date_hist": pl.Date,
            },
            orient="row",
        )
        df = df.join(bounds, on="scheme_code", how="left")
        df = df.with_columns(
            pl.coalesce(["inception_date_right", "inception_date"]).alias("inception_date"),
            pl.coalesce(["last_seen_date_hist", "last_seen_date"]).alias("last_seen_date"),
        )
        if "inception_date_right" in df.columns:
            df = df.drop("inception_date_right")
        if "last_seen_date_hist" in df.columns:
            df = df.drop("last_seen_date_hist")

    df = df.with_columns(
        pl.col("scheme_code").cast(pl.Utf8),
        pl.col("inception_date").cast(pl.Date),
        pl.col("last_seen_date").cast(pl.Date),
    )

    # Sanity reconciliation: any active DIRECT scheme in a rankable category
    # whose option is still UNKNOWN is silently outside the ranked universe.
    # Expected near-zero after the cumulative/spelled-out-IDCW/single-option
    # fixes; growth here means AMFI option-naming drift.
    unknown_direct = _direct_unknown_rankable(df)
    if unknown_direct.height:
        log.warning(
            "scheme_master.direct_unknown_option",
            count=unknown_direct.height,
            schemes=[f"{code}: {name}" for code, name in unknown_direct.rows()],
        )

    # F-12 category-relabel guard. First surface every unmatched raw AMFI
    # category string (with scheme counts) at warning level, then halt if a
    # currently-populated canonical category would vanish from the new build
    # — TRUNCATE-free diff-sync or not, ranking a silently shrunken universe
    # violates the fail-fast invariant.
    if unmatched_raw:
        log.warning(
            "scheme_master.unmatched_categories",
            n_distinct=len(unmatched_raw),
            n_schemes=sum(unmatched_raw.values()),
            categories=dict(
                sorted(unmatched_raw.items(), key=lambda kv: -kv[1])
            ),
        )
    new_counts = {
        cat: n
        for cat, n in df.filter(pl.col("canonical_category").is_not_null())
        .group_by("canonical_category")
        .len()
        .iter_rows()
    }
    check_category_disappearance(
        new_counts, _existing_active_category_counts(), dict(unmatched_raw)
    )

    w.sync_scheme_master(df)
    # D2 point-in-time history: record the post-sync state (including
    # inactive/departed rows) under today's date; same-day re-builds replace
    # the partition.
    w.snapshot_scheme_master(today)
    log.info("scheme_master.built", rows=df.height)
    return df
