"""B12: adapter registry == package contents, for both ingest packages.

The managers package used to maintain its adapter imports by hand, which
left three fully-written adapters (trust, jio_blackrock, the_wealth_company)
silently dormant — written, never imported, ``@register_adapter`` never run.
Both packages now pkgutil-auto-import every non-underscore module; these
tests fail if any module defining an adapter class is not importable and
registered, so an adapter can never go dormant again.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import mfs.ingest.holdings as holdings_pkg
import mfs.ingest.managers as managers_pkg
from mfs.ingest.holdings._base import HoldingsAdapter
from mfs.ingest.managers._base import ManagerAdapter


def _adapter_slugs_in_package(pkg, base_cls) -> set[str]:
    """Walk the package's non-underscore modules; collect the amc_slug of
    every adapter class DEFINED there (imports of other modules' classes
    are excluded via __module__)."""
    slugs: set[str] = set()
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{mod_info.name}")
        for obj in vars(mod).values():
            if (
                inspect.isclass(obj)
                and issubclass(obj, base_cls)
                and obj is not base_cls
                and obj.__module__ == mod.__name__
                and getattr(obj, "amc_slug", None)
            ):
                slugs.add(obj.amc_slug)
    return slugs


def test_managers_registry_equals_package_contents():
    expected = _adapter_slugs_in_package(managers_pkg, ManagerAdapter)
    assert expected, "package walk found no manager adapters — walker broken"
    assert set(managers_pkg.registered_adapters()) == expected


def test_holdings_registry_equals_package_contents():
    expected = _adapter_slugs_in_package(holdings_pkg, HoldingsAdapter)
    assert expected, "package walk found no holdings adapters — walker broken"
    assert set(holdings_pkg.registered_adapters()) == expected


def test_formerly_dormant_manager_adapters_are_registered():
    registered = set(managers_pkg.registered_adapters())
    assert {"trust", "jio_blackrock", "the_wealth_company"} <= registered


def test_known_blocked_ledger_keys_are_registered_slugs():
    # Every known-blocked entry must reference a real, registered adapter —
    # a stale ledger key would silently excuse a slug that no longer runs.
    assert set(managers_pkg._KNOWN_BLOCKED) <= set(managers_pkg.registered_adapters())


def test_known_blocked_ledger_carries_reasons():
    for slug, reason in managers_pkg._KNOWN_BLOCKED.items():
        assert isinstance(reason, str) and reason.strip(), slug
