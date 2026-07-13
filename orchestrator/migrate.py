import hashlib
import os
import re

from db import get_session
from logging_config import get_logger
from sqlalchemy import text

logger = get_logger("migrate")

MIGRATIONS_DIR = os.environ.get("MIGRATIONS_DIR", "/app/db/migrations")

_MIGRATION_FILE_RE = re.compile(r"^(\d+)_.*\.sql$")


def ensure_tracking_table(config):
    """Create the schema_migrations tracking table if it doesn't exist."""
    sql = text("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            checksum TEXT
        )
    """)
    with get_session(config) as session:
        session.execute(sql)
    logger.info("tracking_table_ensured")


def get_applied_versions(config):
    """Return a set of already-applied migration version strings."""
    with get_session(config) as session:
        result = session.execute(
            text("SELECT version FROM schema_migrations")
        )
        return {row[0] for row in result}


def compute_checksum(filepath):
    """Compute SHA256 hex digest, returning the first 16 characters."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]


def apply_migration(version, filepath, config):
    """Read SQL file, execute it, and record the migration."""
    with open(filepath) as f:
        sql_content = f.read()

    checksum = compute_checksum(filepath)

    with get_session(config) as session:
        session.execute(text(sql_content))
        session.execute(
            text(
                "INSERT INTO schema_migrations (version, checksum) "
                "VALUES (:version, :checksum)"
            ),
            {"version": version, "checksum": checksum},
        )

    logger.info("migration_applied", version=version, checksum=checksum)


def run_migrations(config):
    """Apply all pending migrations in sorted order.

    Returns a list of version strings that were applied in this run.
    """
    migrations_dir = os.path.normpath(MIGRATIONS_DIR)
    if not os.path.isdir(migrations_dir):
        raise FileNotFoundError(
            f"Migration inventory directory does not exist: {migrations_dir}"
        )

    ensure_tracking_table(config)
    applied = get_applied_versions(config)

    files = sorted(os.listdir(migrations_dir))
    pending = []
    for filename in files:
        match = _MIGRATION_FILE_RE.match(filename)
        if not match:
            continue
        version = match.group(1)
        if version not in applied:
            pending.append((version, filename))

    applied_now = []
    for version, filename in pending:
        filepath = os.path.join(migrations_dir, filename)
        apply_migration(version, filepath, config)
        applied_now.append(version)

    if applied_now:
        logger.info(
            "migrations_applied",
            count=len(applied_now),
            versions=applied_now,
        )
    else:
        logger.info("no_pending_migrations")

    return applied_now
