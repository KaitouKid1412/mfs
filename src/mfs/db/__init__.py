"""Postgres data layer.

Replaces the parquet curated/metrics datasets with Postgres tables. Schema is
declared in `schema.sql`. Connection strings come from `MFS_DB_URL` (default
`postgresql:///mfs` — local peer auth as the current Unix user).

Migration from the existing parquet layout is one-shot: `mfs db init` creates
tables, `mfs db migrate` bulk-loads via COPY, `mfs db verify` cross-checks
row counts and date bounds before any parquet is deleted.
"""

from __future__ import annotations
