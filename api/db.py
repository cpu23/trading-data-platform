from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from config import load_config
from logging_config import get_logger

logger = get_logger("db")

_engine = None
_SessionFactory = None


def get_engine(config: dict | None = None):
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


def _get_session_factory(config: dict | None = None):
    global _SessionFactory
    if _SessionFactory is not None:
        return _SessionFactory
    engine = get_engine(config)
    _SessionFactory = sessionmaker(bind=engine)
    return _SessionFactory


@contextmanager
def get_session(config: dict | None = None):
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


def query_one(sql, params: dict | None = None, config: dict | None = None) -> dict | None:
    with get_session(config) as session:
        result = session.execute(text(sql), params or {})
        row = result.fetchone()
    if row is None:
        return None
    return dict(row._mapping)


def query_many(sql, params: dict | None = None, config: dict | None = None) -> list[dict]:
    with get_session(config) as session:
        result = session.execute(text(sql), params or {})
        return [dict(row._mapping) for row in result]