"""Per-AMC monthly portfolio holdings ingestion package (Phase 3.C).

Parallel to ingest/managers/ but sourced from each AMC's monthly portfolio
disclosure Excels (which carry ISINs) instead of factsheet PDFs (which
don't). See ingest/holdings/_base.py for the adapter contract.

Adapters self-register via @register_adapter. Every non-underscore module
in this package is auto-imported below (pkgutil), so adding an adapter
module is sufficient to register it — no hand-maintained import list to
forget (Phase 6 B12). tests/test_adapter_registry.py enforces
registry == package contents.
"""

from __future__ import annotations

import importlib
import pkgutil

from mfs.ingest.holdings._registry import (
    get_adapter, register_adapter, registered_adapters,
)
from mfs.ingest.holdings._run import run_all, run_for_amc


def _auto_import_adapters() -> None:
    """Import every non-underscore module in this package so each adapter's
    @register_adapter decorator runs. Underscore modules (_base, _generic,
    _registry, _run) are infrastructure, not adapters."""
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{mod.name}")


_auto_import_adapters()

__all__ = [
    "run_all", "run_for_amc",
    "register_adapter", "registered_adapters", "get_adapter",
]
