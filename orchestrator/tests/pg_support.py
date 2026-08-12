"""Shared real-PostgreSQL integration support.

Env-gated by ``TEST_DATABASE_URL``: tests that import this module skip
cleanly when it is unset (local default), and run against a disposable
database when CI sets it.  The database is self-provisioned from
``db/init/*.sql`` plus the full ``db/migrations`` inventory, so the tests
exercise the same DDL production applies.  Tables are truncated between
tests; the schema is rebuilt once per module.

Shared with the budget (045) and reaction/analytics (044) integration tests
so one CI postgres service + env var serves every slice.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DIR = REPO_ROOT / "db" / "init"
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

ENV_VAR = "TEST_DATABASE_URL"
ALLOW_RESET_ENV = "TEST_DATABASE_ALLOW_RESET"

# Tables the durable-lifecycle tests touch; truncate between tests.
LIFECYCLE_TABLES = (
    "cycle_runs",
    "operation_jobs",
    "analysis_jobs",
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
    """Rebuild the public schema from init scripts + migrations.

    Destructive by design; guarded by :func:`assert_safe_database` so a
    mistyped environment variable can never reset a production database.
    """
    if url is None:
        db = config.get("database", {})
        url = (
            f"postgresql://{db.get('user', '')}@{db.get('host', 'localhost')}:"
            f"{db.get('port', 5432)}/{db.get('name', '')}"
        )
    assert_safe_database(url, allow_reset=allow_reset_enabled())
    import migrate
    from db import get_engine

    engine = get_engine(config)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    # Dropping TimescaleDB objects leaves its shared library loaded in this
    # backend. Recreate the extension on a fresh connection as required by
    # TimescaleDB, rather than reusing SQLAlchemy's pooled reset connection.
    engine.dispose()
    for path in sorted(INIT_DIR.glob("*.sql")):
        with engine.begin() as connection:
            connection.execute(text(path.read_text()))
    migrate.MIGRATIONS_DIR = str(MIGRATIONS_DIR)
    migrate.run_migrations(config)


def truncate(config: dict, tables: tuple[str, ...] = LIFECYCLE_TABLES) -> None:
    """Empty the touched tables between tests (FK-safe via CASCADE)."""
    from db import get_session

    with get_session(config) as session:
        session.execute(
            text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        )
