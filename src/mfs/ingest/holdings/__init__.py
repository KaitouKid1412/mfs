"""Per-AMC monthly portfolio holdings ingestion package (Phase 3.C).

Parallel to ingest/managers/ but sourced from each AMC's monthly portfolio
disclosure Excels (which carry ISINs) instead of factsheet PDFs (which
don't). See ingest/holdings/_base.py for the adapter contract.
"""

from __future__ import annotations

from mfs.ingest.holdings._registry import (
    get_adapter, register_adapter, registered_adapters,
)
from mfs.ingest.holdings._run import run_all, run_for_amc

# Side-effect imports so adapters self-register on package import.
from mfs.ingest.holdings import hdfc  # noqa: F401
from mfs.ingest.holdings import nippon  # noqa: F401
from mfs.ingest.holdings import sbi  # noqa: F401

# Phase 5: per-AMC SEBI monthly-portfolio holdings adapters (generic parser).
from mfs.ingest.holdings import absl  # noqa: F401
from mfs.ingest.holdings import axis  # noqa: F401
from mfs.ingest.holdings import bajaj_finserv  # noqa: F401
from mfs.ingest.holdings import bandhan  # noqa: F401
from mfs.ingest.holdings import bank_of_india  # noqa: F401
from mfs.ingest.holdings import baroda_bnp  # noqa: F401
from mfs.ingest.holdings import canara_robeco  # noqa: F401
from mfs.ingest.holdings import dsp  # noqa: F401
from mfs.ingest.holdings import edelweiss  # noqa: F401
from mfs.ingest.holdings import franklin  # noqa: F401
from mfs.ingest.holdings import groww  # noqa: F401
from mfs.ingest.holdings import helios  # noqa: F401
from mfs.ingest.holdings import hsbc  # noqa: F401
from mfs.ingest.holdings import icici_pru  # noqa: F401
from mfs.ingest.holdings import invesco  # noqa: F401
from mfs.ingest.holdings import iti  # noqa: F401
from mfs.ingest.holdings import kotak  # noqa: F401
from mfs.ingest.holdings import lic  # noqa: F401
from mfs.ingest.holdings import mahindra_manulife  # noqa: F401
from mfs.ingest.holdings import mirae  # noqa: F401
from mfs.ingest.holdings import motilal_oswal  # noqa: F401
from mfs.ingest.holdings import navi  # noqa: F401
from mfs.ingest.holdings import pgim_india  # noqa: F401
from mfs.ingest.holdings import quant  # noqa: F401
from mfs.ingest.holdings import quantum  # noqa: F401
from mfs.ingest.holdings import samco  # noqa: F401
from mfs.ingest.holdings import shriram  # noqa: F401
from mfs.ingest.holdings import sundaram  # noqa: F401
from mfs.ingest.holdings import tata  # noqa: F401
from mfs.ingest.holdings import taurus  # noqa: F401
from mfs.ingest.holdings import the_wealth_company  # noqa: F401
from mfs.ingest.holdings import union  # noqa: F401
from mfs.ingest.holdings import uti  # noqa: F401
from mfs.ingest.holdings import whiteoak_capital  # noqa: F401

# Phase 5 repair pass: previously-uncracked tail AMCs.
from mfs.ingest.holdings import abakkus  # noqa: F401
from mfs.ingest.holdings import capitalmind  # noqa: F401
from mfs.ingest.holdings import jio_blackrock  # noqa: F401
from mfs.ingest.holdings import jm_financial  # noqa: F401
from mfs.ingest.holdings import nj  # noqa: F401
from mfs.ingest.holdings import old_bridge  # noqa: F401
from mfs.ingest.holdings import ppfas  # noqa: F401
from mfs.ingest.holdings import trust  # noqa: F401
from mfs.ingest.holdings import unifi  # noqa: F401

__all__ = [
    "run_all", "run_for_amc",
    "register_adapter", "registered_adapters", "get_adapter",
]
