"""Central pipeline error types.

Raise these (not bare RuntimeError) anywhere the pipeline must halt rather than
proceed with partial / stale / synthetic data. Compute and rank stages refuse
to run when these are unresolved upstream.
"""

from __future__ import annotations

from pathlib import Path


class PipelineError(RuntimeError):
    """Base class for all errors that should halt the pipeline."""


class IngestError(PipelineError):
    """An ingest stage failed (network exhausted, parse empty, etc.)."""


class StatementDateMismatchError(IngestError):
    """A month-keyed artifact's printed 'AS ON <date>' statement month does
    not match the month it was requested as. Caching or ingesting it would
    poison the month-keyed cache and the DB partition (e.g. quant serving
    April files for a May request). The artifact must be evicted, never
    written."""

    def __init__(
        self, artifact_path: Path, expected_ym: str, found_yms: set[str],
    ) -> None:
        self.artifact_path = artifact_path
        self.expected_ym = expected_ym
        self.found_yms = found_yms
        found = ", ".join(sorted(found_yms)) if found_yms else "none"
        super().__init__(
            f"{artifact_path}: statement-date mismatch — expected month "
            f"{expected_ym}, found {found}"
        )


class FreshnessError(PipelineError):
    """A curated dataset is stale or missing. Compute/rank must not run."""


class CoverageError(PipelineError):
    """A BLOCKING data-coverage contract failed (empty / stale / short-history /
    catastrophically low entity coverage). Compute/rank must not run."""
