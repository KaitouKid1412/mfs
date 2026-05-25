"""Registry for per-AMC HoldingsAdapter classes (Phase 3.C).

Mirrors the managers/ registry — adapters self-register via @register_adapter.
"""

from __future__ import annotations

from typing import TypeVar

from mfs.ingest.holdings._base import HoldingsAdapter

_REGISTRY: dict[str, type[HoldingsAdapter]] = {}
T = TypeVar("T", bound=type[HoldingsAdapter])


def register_adapter(cls: T) -> T:
    slug = getattr(cls, "amc_slug", None)
    if not slug:
        raise ValueError(f"{cls.__name__} must define class attr `amc_slug`")
    if slug in _REGISTRY:
        raise ValueError(f"Holdings adapter for amc_slug={slug!r} already registered")
    _REGISTRY[slug] = cls
    return cls


def get_adapter(amc_slug: str) -> HoldingsAdapter:
    cls = _REGISTRY.get(amc_slug)
    if cls is None:
        raise KeyError(
            f"No holdings adapter registered for amc_slug={amc_slug!r}. "
            f"Registered: {sorted(_REGISTRY.keys())}"
        )
    return cls()


def registered_adapters() -> list[str]:
    return sorted(_REGISTRY.keys())
