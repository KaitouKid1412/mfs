"""AMC factsheet ingestion package.

Each AMC has its own adapter (one module per AMC) that:
  1. fetches the latest monthly factsheet PDF,
  2. parses out per-scheme holdings and PTR signals,
  3. fuzzy-matches scheme names to scheme_codes against scheme_master.

Adapters self-register via the @register_adapter decorator in _registry.
Every non-underscore module in this package is auto-imported below
(pkgutil), so simply adding an adapter module registers it — a written
adapter can never again sit silently dormant because someone forgot to
extend a hand-maintained import list (which is exactly how trust,
jio_blackrock and the_wealth_company stayed inactive until Phase 6 B12).
tests/test_adapter_registry.py enforces registry == package contents.

The top-level run_for_amc / run_all entrypoints dispatch to the registered
adapters and write results into holdings_monthly and
portfolio_turnover_monthly.

Package path is retained as ``mfs.ingest.managers`` for backward
compatibility — manager-tenure extraction was removed when the user opted
to verify manager tenure manually for the Stage 2 survivor set.
"""

from __future__ import annotations

import importlib
import pkgutil

from mfs.ingest.managers._registry import register_adapter, registered_adapters
from mfs.ingest.managers._run import run_all, run_for_amc

# Known-walled AMCs (B12 BLOCKED ledger): these adapters are registered and
# run — they fail LOUDLY per-AMC inside run_all's fault isolation — but the
# failure is an external wall, not a regression. Gate B / run summaries can
# consult this ledger to distinguish 'known-walled' from 'newly broken'.
# Activation decisions (2026-06, Phase 6 B12):
#   * trust              — registered; fetch raises IngestError because the
#                          trustmf.com WAF serves a 487-byte React SPA shell
#                          (HTTP 200, text/html) for every PDF path. B1
#                          content validation guarantees the shell is never
#                          cached. Recorded per-AMC failure is the intended
#                          outcome.
#   * jio_blackrock      — registered; fetch raises IngestError unless an
#                          operator has dropped the resolved PDF in the cache,
#                          because the factsheet index sits behind an
#                          auth-gated Strapi API and the CDN filenames are
#                          opaque random tokens.
#   * the_wealth_company — registered PLAINLY (not in this ledger): its fetch
#                          works and parse_ptr correctly yields zero records
#                          until the AMC starts printing PTR (all funds are
#                          <1y old), which records a clean per-AMC result.
_KNOWN_BLOCKED: dict[str, str] = {
    "trust": (
        "WAF serves an HTML SPA shell (HTTP 200) for all PDF paths "
        "(see module docstring)"
    ),
    "jio_blackrock": (
        "factsheet index behind auth-gated Strapi API; CDN filenames are "
        "opaque tokens not derivable from the data month"
    ),
}


def _auto_import_adapters() -> None:
    """Import every non-underscore module in this package so each adapter's
    @register_adapter decorator runs. Underscore modules (_base, _registry,
    _run, _scheme_match) are infrastructure, not adapters."""
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{mod.name}")


_auto_import_adapters()

__all__ = ["run_all", "run_for_amc", "register_adapter", "registered_adapters"]
