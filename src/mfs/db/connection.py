"""Postgres connection helpers.

Default URL is `postgresql:///mfs` — Unix-socket local connection as the current
shell user. Override via `MFS_DB_URL` in `.env`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager, suppress
from typing import Iterator

import psycopg

from mfs.errors import PipelineError

DEFAULT_DSN = "postgresql:///mfs"

# Arbitrary 32-bit key identifying the pipeline advisory lock ('mfsp').
_PIPELINE_LOCK_KEY = 0x6D667370


def get_dsn() -> str:
    return os.environ.get("MFS_DB_URL", DEFAULT_DSN)


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Open a single Postgres connection scoped to the with-block."""
    conn = psycopg.connect(get_dsn(), autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def server_database() -> str:
    """Return the connected DB name — used by `mfs db status`."""
    with connect() as c:
        return c.execute("SELECT current_database()").fetchone()[0]


@contextmanager
def pipeline_lock() -> Iterator[None]:
    """Hold a session-scoped Postgres advisory lock for one pipeline run.

    Prevents two concurrent ``mfs pipeline`` runs from racing the same partitions
    and truncate-reloads. The lock is bound to a dedicated connection held open
    for the run's duration; Postgres releases it automatically when that
    connection closes — including if the process is killed — so a crashed run
    never strands the lock. Raises ``PipelineError`` if another run holds it.
    """
    conn = psycopg.connect(get_dsn(), autocommit=True)
    got = False
    try:
        got = conn.execute(
            "SELECT pg_try_advisory_lock(%s)", (_PIPELINE_LOCK_KEY,)
        ).fetchone()[0]
        if not got:
            raise PipelineError(
                "Another `mfs pipeline` run is already in progress (advisory lock "
                "held). Refusing to start a concurrent run — wait for it to finish "
                "or stop it first."
            )
        yield
    finally:
        if got:
            # Best-effort explicit unlock; the lock also frees when conn closes.
            with suppress(Exception):
                conn.execute("SELECT pg_advisory_unlock(%s)", (_PIPELINE_LOCK_KEY,))
        conn.close()
