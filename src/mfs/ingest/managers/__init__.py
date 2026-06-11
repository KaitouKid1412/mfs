"""AMC factsheet ingestion package.

Each AMC has its own adapter (one module per AMC) that:
  1. fetches the latest monthly factsheet PDF,
  2. parses out per-scheme holdings, PTR, and AUM signals,
  3. fuzzy-matches scheme names to scheme_codes against scheme_master.

Adapters self-register via the @register_adapter decorator in _registry.
The top-level run_for_amc / run_all entrypoints below dispatch to the
registered adapters and write results into holdings_monthly,
portfolio_turnover_monthly, and scheme_aum_monthly.

Package path is retained as ``mfs.ingest.managers`` for backward
compatibility — manager-tenure extraction was removed when the user opted
to verify manager tenure manually for the Stage 2 survivor set.
"""

from __future__ import annotations

from mfs.ingest.managers._registry import register_adapter, registered_adapters
from mfs.ingest.managers._run import run_all, run_for_amc

# Side-effect imports so adapters register on package import.
from mfs.ingest.managers import hdfc  # noqa: F401
from mfs.ingest.managers import sbi  # noqa: F401
from mfs.ingest.managers import nippon  # noqa: F401
from mfs.ingest.managers import absl  # noqa: F401
from mfs.ingest.managers import icici_pru  # noqa: F401
from mfs.ingest.managers import mirae  # noqa: F401
from mfs.ingest.managers import kotak  # noqa: F401
from mfs.ingest.managers import uti  # noqa: F401
from mfs.ingest.managers import dsp  # noqa: F401
from mfs.ingest.managers import bandhan  # noqa: F401
from mfs.ingest.managers import axis  # noqa: F401
from mfs.ingest.managers import tata  # noqa: F401
from mfs.ingest.managers import franklin  # noqa: F401
from mfs.ingest.managers import edelweiss  # noqa: F401
from mfs.ingest.managers import hsbc  # noqa: F401
from mfs.ingest.managers import baroda_bnp  # noqa: F401
from mfs.ingest.managers import invesco  # noqa: F401
from mfs.ingest.managers import motilal_oswal  # noqa: F401
from mfs.ingest.managers import sundaram  # noqa: F401
from mfs.ingest.managers import groww  # noqa: F401
from mfs.ingest.managers import quant  # noqa: F401
from mfs.ingest.managers import whiteoak_capital  # noqa: F401
from mfs.ingest.managers import lic  # noqa: F401
from mfs.ingest.managers import union  # noqa: F401
from mfs.ingest.managers import mahindra_manulife  # noqa: F401
from mfs.ingest.managers import bank_of_india  # noqa: F401
from mfs.ingest.managers import canara_robeco  # noqa: F401
from mfs.ingest.managers import iti  # noqa: F401
from mfs.ingest.managers import pgim_india  # noqa: F401

# Phase 5 PTR adapters (AMCs that previously had no factsheet adapter).
from mfs.ingest.managers import bajaj_finserv  # noqa: F401
from mfs.ingest.managers import jm_financial  # noqa: F401
from mfs.ingest.managers import samco  # noqa: F401
from mfs.ingest.managers import taurus  # noqa: F401
from mfs.ingest.managers import helios  # noqa: F401
from mfs.ingest.managers import shriram  # noqa: F401
from mfs.ingest.managers import navi  # noqa: F401
from mfs.ingest.managers import quantum  # noqa: F401
from mfs.ingest.managers import ppfas  # noqa: F401
from mfs.ingest.managers import nj  # noqa: F401
from mfs.ingest.managers import old_bridge  # noqa: F401
from mfs.ingest.managers import capitalmind  # noqa: F401

__all__ = ["run_all", "run_for_amc", "register_adapter", "registered_adapters"]
