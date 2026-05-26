"""Registry of AMC-specific manager adapters."""

from __future__ import annotations

from typing import Callable, TypeVar

from mfs.ingest.managers._base import ManagerAdapter

_REGISTRY: dict[str, type[ManagerAdapter]] = {}
T = TypeVar("T", bound=type[ManagerAdapter])


def register_adapter(cls: T) -> T:
    """Class decorator: register an adapter under its `amc_slug`."""
    slug = getattr(cls, "amc_slug", None)
    if not slug:
        raise ValueError(f"{cls.__name__} must define class attr `amc_slug`")
    if slug in _REGISTRY:
        raise ValueError(f"Adapter for amc_slug={slug!r} already registered")
    _REGISTRY[slug] = cls
    return cls


def get_adapter(amc_slug: str) -> ManagerAdapter:
    """Instantiate the adapter for amc_slug; raises if not registered."""
    cls = _REGISTRY.get(amc_slug)
    if cls is None:
        raise KeyError(
            f"No manager adapter registered for amc_slug={amc_slug!r}. "
            f"Registered: {sorted(_REGISTRY.keys())}"
        )
    return cls()


def registered_adapters() -> list[str]:
    """All registered amc_slugs (sorted)."""
    return sorted(_REGISTRY.keys())
