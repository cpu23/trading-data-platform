from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from config import AppConfig, load_config
from logging_config import get_logger

logger = get_logger("db")
_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine(config: AppConfig | None = None) -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    if config is None:
        config = load_config()

    db_config = config["database"]
    url = (
        f"postgresql://{db_config['user']}:{db_config['password']}"
        f"@{db_config['host']}:{db_config['port']}/{db_config['name']}"
    )
    _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
    return _engine


def _get_session_factory(config: AppConfig | None = None) -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is not None:
        return _SessionFactory
    engine = get_engine(config)
    _SessionFactory = sessionmaker(bind=engine)
    return _SessionFactory


@contextmanager
def get_session(config: AppConfig | None = None) -> Generator[Session, None, None]:
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
