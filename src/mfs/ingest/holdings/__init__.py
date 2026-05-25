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

__all__ = [
    "run_all", "run_for_amc",
    "register_adapter", "registered_adapters", "get_adapter",
]
