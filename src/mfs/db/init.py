"""mfs db init — create the DB if missing, then apply schema.sql."""

from __future__ import annotations

from pathlib import Path

import psycopg

from mfs.db.connection import get_dsn
from mfs.utils.logging import get_logger

log = get_logger(__name__)

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def _parse_target_db(dsn: str) -> tuple[str, str]:
    """Return (db_name, dsn_to_postgres_admin_db) for CREATE DATABASE.

    psycopg.conninfo is more reliable than manual URL parsing for this.
    """
    parsed = psycopg.conninfo.conninfo_to_dict(dsn)
    db = parsed.get("dbname") or parsed.get("database") or ""
    if not db:
        raise RuntimeError(f"DSN {dsn!r} has no dbname; cannot create database")
    admin_parts = {**parsed, "dbname": "postgres"}
    admin_dsn = psycopg.conninfo.make_conninfo(**admin_parts)
    return db, admin_dsn


def ensure_database(dsn: str | None = None) -> None:
    """If the target database doesn't exist, create it. No-op if it exists."""
    dsn = dsn or get_dsn()
    db_name, admin_dsn = _parse_target_db(dsn)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
        ).fetchone()
        if row:
            log.info("db.exists", db=db_name)
            return
        # Identifier-quote the DB name. We trust DSN-derived values but keep
        # this safe in case the user passes something weird.
        conn.execute(
            psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(db_name))
        )
        log.info("db.created", db=db_name)


def apply_schema(dsn: str | None = None) -> None:
    """Apply schema.sql idempotently."""
    dsn = dsn or get_dsn()
    sql = SCHEMA_FILE.read_text()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    log.info("db.schema_applied", file=str(SCHEMA_FILE))


def init(dsn: str | None = None) -> None:
    ensure_database(dsn)
    apply_schema(dsn)
