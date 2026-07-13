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


def get_applied_migrations(config) -> dict[str, str | None]:
    """Return applied migration versions mapped to their stored checksums."""
    with get_session(config) as session:
        result = session.execute(
            text("SELECT version, checksum FROM schema_migrations")
        )
        return {row[0]: row[1] for row in result}


def get_applied_versions(config):
    """Return a set of already-applied migration version strings."""
    return set(get_applied_migrations(config))


def compute_checksum(filepath):
    """Compute SHA256 hex digest, returning the first 16 characters."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]


def backfill_checksum(version, filepath, config):
    """Populate a missing historical checksum in its own transaction."""
    checksum = compute_checksum(filepath)
    with get_session(config) as session:
        session.execute(
            text(
                "UPDATE schema_migrations SET checksum = :checksum "
                "WHERE version = :version AND checksum IS NULL"
            ),
            {"version": version, "checksum": checksum},
        )
    logger.info(
        "migration_checksum_backfilled",
        version=version,
        path=filepath,
        checksum=checksum,
    )


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


def run_migrations(config, allow_checksum_backfill: bool = False):
    """Verify migration history and apply pending migrations in sorted order.

    Historical null checksums are rejected unless ``allow_checksum_backfill``
    is explicitly enabled for this invocation.

    Returns a list of version strings that were applied in this run.
    """
    migrations_dir = os.path.normpath(MIGRATIONS_DIR)
    if not os.path.isdir(migrations_dir):
        raise FileNotFoundError(
            f"Migration inventory directory does not exist: {migrations_dir}"
        )

    ensure_tracking_table(config)
    applied = get_applied_migrations(config)

    files = sorted(os.listdir(migrations_dir))
    inventory = {}
    for filename in files:
        match = _MIGRATION_FILE_RE.match(filename)
        if not match:
            continue
        version = match.group(1)
        filepath = os.path.join(migrations_dir, filename)
        if version in inventory:
            raise RuntimeError(
                f"duplicate migration version {version}: "
                f"{inventory[version]} and {filepath}"
            )
        inventory[version] = filepath

    for version, stored_checksum in applied.items():
        if version not in inventory:
            raise RuntimeError(
                f"Applied migration version {version} is missing from {migrations_dir}"
            )
        filepath = inventory[version]
        if stored_checksum is None:
            if not allow_checksum_backfill:
                raise RuntimeError(
                    f"Applied migration {version} at {filepath} has a null checksum"
                )
            backfill_checksum(version, filepath, config)
        else:
            disk_checksum = compute_checksum(filepath)
            if stored_checksum != disk_checksum:
                raise RuntimeError(
                    f"Checksum mismatch for applied migration {version} at {filepath}"
                )

    pending = []
    for version, filepath in inventory.items():
        if version not in applied:
            pending.append((version, filepath))

    applied_now = []
    for version, filepath in pending:
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
