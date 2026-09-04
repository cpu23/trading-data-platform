import threading
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any

from api_logging import get_logger
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from config import AppConfig, load_config

logger = get_logger("db")
_engine: Engine | None = None
_engine_url: URL | None = None
_SessionFactory: sessionmaker[Session] | None = None
_database_cache_lock = threading.RLock()


def _database_url(db_config: Mapping[str, Any]) -> URL:
    """Build a SQLAlchemy URL with proper credential escaping."""
    return URL.create(
        "postgresql+psycopg2",
        username=db_config["user"],
        password=db_config.get("password", ""),
        host=db_config["host"],
        port=db_config.get("port", 5432),
        database=db_config["name"],
    )


def get_engine(config: AppConfig | Mapping[str, Any] | None = None) -> Engine:
    """Return the engine for the current immutable configuration snapshot.

    A committed profile can change database coordinates while this API process
    remains alive. Keying the cache by the structured URL prevents readiness
    and request handlers from silently continuing against the previous
    database after the configuration version advances.
    """
    global _engine, _engine_url, _SessionFactory
    if config is None:
        config = load_config()
    database_url = _database_url(config["database"])

    with _database_cache_lock:
        if _engine is not None and _engine_url == database_url:
            return _engine
        previous = _engine
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            connect_args={"connect_timeout": 3},
        )
        _engine = engine
        _engine_url = database_url
        _SessionFactory = sessionmaker(bind=engine)
    if previous is not None:
        # dispose() closes idle connections; already checked-out sessions retain
        # their connection until their request reaches its normal boundary.
        previous.dispose()
    return engine


def check_connection(config: AppConfig | Mapping[str, Any] | None = None) -> bool:
    """Bounded connectivity check used by readiness endpoints."""
    try:
        with get_session(config) as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error(
            "db_connection_failed", action="db_check", error_type=type(exc).__name__
        )
        return False


def _get_session_factory(
    config: AppConfig | Mapping[str, Any] | None = None,
) -> sessionmaker[Session]:
    # get_engine() compares the active URL on every session boundary and
    # atomically replaces both caches when a committed profile changes it.
    engine = get_engine(config)
    with _database_cache_lock:
        if _SessionFactory is None:
            # Defensive only: get_engine() normally installs this factory.
            return sessionmaker(bind=engine)
        return _SessionFactory


@contextmanager
def get_session(
    config: AppConfig | Mapping[str, Any] | None = None,
) -> Generator[Session, None, None]:
    factory = _get_session_factory(config)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def query_one(
    sql: str,
    params: Mapping[str, Any] | None = None,
    config: AppConfig | None = None,
) -> dict[str, Any] | None:
    with get_session(config) as session:
        result = session.execute(text(sql), params or {})
        row = result.fetchone()
    if row is None:
        return None
    return dict(row._mapping)


def query_many(
    sql: str,
    params: Mapping[str, Any] | None = None,
    config: AppConfig | None = None,
) -> list[dict[str, Any]]:
    with get_session(config) as session:
        result = session.execute(text(sql), params or {})
        return [dict(row._mapping) for row in result]
