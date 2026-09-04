"""Shared real-PostgreSQL integration support.

Env-gated by ``TEST_DATABASE_URL``: tests skip when it is unset and run against
a disposable database when CI sets it. The database is provisioned from the
authoritative ``db/schema.sql`` and tables are truncated between tests.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"

ENV_VAR = "TEST_DATABASE_URL"
ALLOW_RESET_ENV = "TEST_DATABASE_ALLOW_RESET"

# Tables the durable-lifecycle tests touch; truncate between tests.
LIFECYCLE_TABLES = (
    "cycle_runs",
    "jobs",
    "role_heartbeats",
    "quote_state",
    "event_outbox",
    "market_events",
)

_SAFE_DB_NAME_RE = re.compile(r"test|ci", re.IGNORECASE)
_UNSAFE_DB_NAMES = frozenset(
    {"trading_data", "postgres", "template", "template0", "template1"}
)


def database_url() -> str | None:
    value = os.environ.get(ENV_VAR)
    return value.strip() if value and value.strip() else None


def allow_reset_enabled() -> bool:
    return os.environ.get(ALLOW_RESET_ENV, "").strip().lower() in {"1", "true", "yes"}


def assert_safe_database(url: str, *, allow_reset: bool = False) -> None:
    """Refuse destructive schema resets unless the target is clearly disposable.

    Guards against a mistyped TEST_DATABASE_URL destroying production: the
    database name must clearly contain ``test``/``ci`` (and must not be a
    known production name), and a non-loopback host requires an explicit
    ``TEST_DATABASE_ALLOW_RESET=1`` confirmation.
    """
    parts = urlsplit(url)
    db_name = (parts.path or "/").lstrip("/") or ""
    host = parts.hostname or ""
    if db_name.lower() in _UNSAFE_DB_NAMES:
        raise RuntimeError(
            f"refusing to reset database '{db_name}': name matches a known "
            "production database; point TEST_DATABASE_URL at a disposable test DB"
        )
    if not _SAFE_DB_NAME_RE.search(db_name):
        raise RuntimeError(
            f"refusing to reset database '{db_name}': the name must clearly "
            "contain test/ci (for example trading_data_test)"
        )
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if not loopback and not allow_reset:
        raise RuntimeError(
            f"refusing to reset non-loopback database at '{host}': set "
            f"{ALLOW_RESET_ENV}=1 to confirm this is a disposable test database"
        )


def parse_config(url: str) -> dict:
    """Build an orchestrator config dict from a postgres:// URL."""
    parts = urlsplit(url)
    return {
        "database": {
            "host": parts.hostname or "localhost",
            "port": parts.port or 5432,
            "name": (parts.path or "/").lstrip("/") or "trading_data",
            "user": parts.username or "",
            "password": parts.password or "",
        }
    }


def require_postgres() -> str:
    """Return TEST_DATABASE_URL or skip the calling test module."""
    url = database_url()
    if url is None:
        raise unittest.SkipTest(
            f"{ENV_VAR} not set; skipping PostgreSQL integration test"
        )
    return url


def provision(config: dict, url: str | None = None) -> None:
    """Rebuild a disposable database from the authoritative schema."""
    if url is None:
        db = config.get("database", {})
        url = (
            f"postgresql://{db.get('user', '')}@{db.get('host', 'localhost')}:"
            f"{db.get('port', 5432)}/{db.get('name', '')}"
        )
    assert_safe_database(url, allow_reset=allow_reset_enabled())
    from db import get_engine

    engine = get_engine(config)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    # Dropping TimescaleDB objects leaves its shared library loaded in this
    # backend. Recreate the extension on a fresh connection as required by
    # TimescaleDB, rather than reusing SQLAlchemy's pooled reset connection.
    engine.dispose()
    with engine.begin() as connection:
        raw_connection = connection.connection.driver_connection
        with raw_connection.cursor() as cursor:
            cursor.execute(SCHEMA_PATH.read_text())


def truncate(config: dict, tables: tuple[str, ...] = LIFECYCLE_TABLES) -> None:
    """Empty the touched tables between tests (FK-safe via CASCADE)."""
    from db import get_session

    with get_session(config) as session:
        session.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
