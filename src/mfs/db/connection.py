"""Postgres connection helpers.

Default URL is `postgresql:///mfs` — Unix-socket local connection as the current
shell user. Override via `MFS_DB_URL` in `.env`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg

DEFAULT_DSN = "postgresql:///mfs"


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
