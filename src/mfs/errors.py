"""Central pipeline error types.

Raise these (not bare RuntimeError) anywhere the pipeline must halt rather than
proceed with partial / stale / synthetic data. Compute and rank stages refuse
to run when these are unresolved upstream.
"""

from __future__ import annotations


class PipelineError(RuntimeError):
    """Base class for all errors that should halt the pipeline."""


class IngestError(PipelineError):
    """An ingest stage failed (network exhausted, parse empty, etc.)."""


class FreshnessError(PipelineError):
    """A curated dataset is stale or missing. Compute/rank must not run."""


class CoverageError(PipelineError):
    """A BLOCKING data-coverage contract failed (empty / stale / short-history /
    catastrophically low entity coverage). Compute/rank must not run."""
