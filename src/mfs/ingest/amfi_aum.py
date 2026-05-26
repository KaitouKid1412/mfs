"""AMFI quarterly Average AUM (AAUM) ingest.

AMFI publishes per-scheme AAUM each quarter via the JSON endpoint behind
https://www.amfiindia.com/aum-data/average-aum. A single GET covers every
SEBI-registered scheme — 1,600+ DIRECT+GROWTH rows in one response —
keyed by AMFI_Code which maps exactly to scheme_master.scheme_code. This
makes the per-AMC factsheet adapters redundant for AUM (they remain the
only source for PTR).

Notes on the data:
  - Values are in INR Lakhs. We divide by 100 to store INR Crore.
  - Cadence is quarterly (Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec) — not monthly.
    We stamp each ingested row with as_of_month = the quarter's last month
    (e.g. Q4 FY 2025-26 = 2026-03). Downstream "latest AUM" queries pick
    the most recent quarter ending on or before the data month.
  - Publication lag is ~30-45 days after quarter end. Q4 (Jan-Mar) typically
    surfaces in early/mid May.
  - source_amc = 'amfi_aaum' so it's distinguishable from factsheet rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from mfs.db import writers as w
from mfs.errors import IngestError
from mfs.io import http
from mfs.utils.logging import get_logger

log = get_logger(__name__)

SCHEMEWISE_URL = "https://www.amfiindia.com/api/average-aum-schemewise"
SOURCE_LABEL = "amfi_aaum"


@dataclass(frozen=True)
class Quarter:
    """An AMFI quarter, addressable by (fyId, periodId)."""

    fy_id: int  # 1 = April 2025 - March 2026, 2 = previous FY, ...
    period_id: int  # 1=Q4 Jan-Mar, 2=Q3 Oct-Dec, 3=Q2 Jul-Sep, 4=Q1 Apr-Jun
    label: str  # e.g. "January - March 2026"
    end_month: date  # last day of quarter, e.g. date(2026, 3, 31)

    @property
    def as_of_month(self) -> date:
        """First day of the quarter's last month, matches scheme_aum_monthly."""
        return self.end_month.replace(day=1)


# Map (fy_offset, q_in_fy) → AMFI ids. fy_offset=0 is the latest FY (id 1);
# q_in_fy uses calendar order (1=Apr-Jun, 4=Jan-Mar), opposite of AMFI's
# periodId numbering. The helper below converts.

# AMFI periodId convention (per-FY): 1=Jan-Mar, 2=Oct-Dec, 3=Jul-Sep, 4=Apr-Jun.
# Calendar-quarter index inside the Indian FY: Q1=Apr-Jun, Q2=Jul-Sep,
# Q3=Oct-Dec, Q4=Jan-Mar. So calendar Q -> periodId mapping is
# {1:4, 2:3, 3:2, 4:1}.
_CAL_Q_TO_PERIOD_ID = {1: 4, 2: 3, 3: 2, 4: 1}
_PERIOD_END_MONTH = {1: (3, 31), 2: (12, 31), 3: (9, 30), 4: (6, 30)}


def quarter_from_label(label: str) -> Quarter:
    """Parse 'Q4-2026' or 'Q1-2025' → Quarter.

    The year in the label is the *calendar year of the quarter's end month*
    (Q4-2026 = Jan-Mar 2026, Q1-2025 = Apr-Jun 2025).
    """
    s = label.strip().upper()
    if not (s.startswith("Q") and "-" in s):
        raise ValueError(f"Bad quarter label {label!r}; expected like 'Q4-2026'")
    q_str, y_str = s[1:].split("-", 1)
    try:
        q = int(q_str)
        year = int(y_str)
    except ValueError as e:
        raise ValueError(f"Bad quarter label {label!r}") from e
    if q not in _CAL_Q_TO_PERIOD_ID:
        raise ValueError(f"Quarter must be 1..4, got {q}")
    # Indian FY: Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec all in FY ending the
    # following March; Q4=Jan-Mar is in FY ending that March.
    # FY label is "April YYYY - March (YYYY+1)". For Q4-2026 (Jan-Mar 2026),
    # FY = April 2025 - March 2026. We discover fyId by hitting the FY-list
    # endpoint.
    fy_end_year = year if q == 4 else year + 1
    fy_label = f"April {fy_end_year - 1} - March {fy_end_year}"
    fy_id = _resolve_fy_id(fy_label)
    period_id = _CAL_Q_TO_PERIOD_ID[q]
    m, d = _PERIOD_END_MONTH[period_id]
    return Quarter(fy_id=fy_id, period_id=period_id, label=fy_label, end_month=date(year, m, d))


def _resolve_fy_id(fy_label: str) -> int:
    """Look up the AMFI fyId for a given 'April YYYY - March YYYY+1' label."""
    raw = http.fetch_bytes("https://www.amfiindia.com/api/average-aum-fundwise")
    payload = json.loads(raw)
    for row in payload.get("data", []):
        if row.get("financial_year") == fy_label:
            return int(row["id"])
    raise IngestError(f"AMFI FY label {fy_label!r} not in available years")


def fetch_quarter(quarter: Quarter, str_type: str = "Typewise") -> dict:
    """GET the schemewise JSON for one quarter (all AMCs, MF_ID=0)."""
    params = {
        "strType": str_type,
        "fyId": quarter.fy_id,
        "periodId": quarter.period_id,
        "MF_ID": 0,
    }
    raw = http.fetch_bytes(SCHEMEWISE_URL, params=params)
    return json.loads(raw)


def parse_payload(payload: dict, quarter: Quarter) -> list[dict]:
    """Flatten the AMC-grouped payload to one row per Direct+Growth scheme_code.

    AMFI publishes per-plan AAUM (one row per Direct/Regular × Growth/IDCW
    combination). The downstream impact-cost metric wants whole-scheme AUM
    because all plans share the same portfolio. So we:

      1. Parse every AMFI row (not just Direct+Growth).
      2. Join AMFI_Code → scheme_master.base_fund_id.
      3. Sum AAUM across all plan rows for each base_fund_id.
      4. Write one row per base_fund_id, keyed by the Direct+Growth
         scheme_code from scheme_master.

    AAUM is converted from lakhs to crore. Rows where the AMFI_Code is not
    in scheme_master (delisted / closed-end / very new) are dropped.
    """
    from mfs.db import queries as q

    sm = q.scheme_master().select(
        ["scheme_code", "base_fund_id", "plan_type", "option_type", "is_active"]
    )
    code_to_fund: dict[str, str] = dict(
        zip(sm["scheme_code"].to_list(), sm["base_fund_id"].to_list())
    )
    # Pick the Direct+Growth scheme_code per base_fund_id as the write key.
    direct_growth = sm.filter(
        (pl.col("plan_type") == "DIRECT")
        & (pl.col("option_type") == "GROWTH")
        & (pl.col("is_active") == True)  # noqa: E712
    )
    fund_to_dg_code: dict[str, str] = dict(
        zip(direct_growth["base_fund_id"].to_list(), direct_growth["scheme_code"].to_list())
    )

    # Sum AAUM (lakhs) across every plan row, grouped by base_fund_id.
    fund_aaum_lakhs: dict[str, float] = {}
    for grp in payload.get("data", []):
        for s in grp.get("schemes", []):
            try:
                amfi_code = str(s["AMFI_Code"])
            except (KeyError, TypeError):
                continue
            base = code_to_fund.get(amfi_code)
            if base is None:
                continue  # AMFI row references a scheme we don't track
            v = (
                s.get("AverageAumForTheMonth", {})
                 .get("ExcludingFundOfFundsDomesticButIncludingFundOfFundsOverseas")
            )
            if v is None or v <= 0:
                continue
            fund_aaum_lakhs[base] = fund_aaum_lakhs.get(base, 0.0) + float(v)

    now = datetime.utcnow()
    as_of_month = quarter.as_of_month
    out: list[dict] = []
    n_no_dg = 0
    for base, total_lakhs in fund_aaum_lakhs.items():
        dg_code = fund_to_dg_code.get(base)
        if dg_code is None:
            # No active Direct+Growth row for this fund — usually a Regular-only
            # legacy scheme or closed-end. Skip (it's not in our ranked universe).
            n_no_dg += 1
            continue
        out.append({
            "scheme_code": dg_code,
            "as_of_month": as_of_month,
            "aum_crore": total_lakhs / 100.0,
            "source_amc": SOURCE_LABEL,
            "computed_at": now,
        })
    if n_no_dg:
        log.info("amfi_aum.parse.no_direct_growth", n_funds_skipped=n_no_dg)
    return out


def ingest_quarter(label: str) -> dict:
    """End-to-end: fetch one quarter, write to scheme_aum_monthly, return summary."""
    q = quarter_from_label(label)
    log.info("amfi_aum.fetch.start", quarter=label, fy_id=q.fy_id, period_id=q.period_id)
    payload = fetch_quarter(q)
    rows = parse_payload(payload, q)
    if not rows:
        raise IngestError(f"AMFI AAUM payload for {label} contained zero DIRECT+GROWTH rows")
    n = w.upsert_scheme_aum(pl.DataFrame(rows))
    log.info(
        "amfi_aum.ingest.done",
        quarter=label,
        as_of_month=str(q.as_of_month),
        rows_parsed=len(rows),
        rows_written=n,
    )
    return {
        "quarter": label,
        "fy_id": q.fy_id,
        "period_id": q.period_id,
        "fy_label": q.label,
        "as_of_month": str(q.as_of_month),
        "rows_parsed": len(rows),
        "rows_written": n,
    }
