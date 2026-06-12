"""Re-export shim — the canonical module moved to ``mfs.ingest._scheme_match``
(F-5: the matcher is shared by the managers AND holdings orchestrators, so it
lives in the parent package instead of being imported across siblings)."""

from mfs.ingest._scheme_match import *  # noqa: F401,F403
from mfs.ingest._scheme_match import (  # noqa: F401 — private names tests rely on
    _ADAPTER_SLUG_TO_SCHEME_MASTER_AMC_CODE,
)
