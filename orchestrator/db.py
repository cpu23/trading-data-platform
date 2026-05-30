from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from logging_config import get_logger

logger = get_logger("db")

_engine = None
_SessionFactory = None


def get_engine(config: dict | None = None):
    global _engine
    if _engine is not None:
        return _engine

    if config is None:
        from config_loader import load_config

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


def upsert_records(
    table_name: str,
    records: list[dict],
    conflict_columns: list[str],
    config: dict | None = None,
) -> int:
    if not records:
        return 0

    import json as _json

    start_ms = _now_ms()
    written = 0

    columns = list(records[0].keys())
    col_list = ", ".join(columns)
    placeholders = ", ".join(f":{col}" for col in columns)
    conflict_cols = ", ".join(conflict_columns)

    update_cols = [c for c in columns if c not in conflict_columns]
    if update_cols:
        update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        sql = (
            f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_clause}"
        )
    else:
        sql = (
            f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_cols}) DO NOTHING"
        )

    stmt = text(sql)

    with get_session(config) as session:
        for record in records:
            try:
                prepared = {}
                for k, v in record.items():
                    if isinstance(v, (dict, list)):
                        prepared[k] = _json.dumps(v)
                    else:
                        prepared[k] = v
                nested = session.begin_nested()
                session.execute(stmt, prepared)
                nested.commit()
                written += 1
            except Exception as exc:
                nested.rollback()
                logger.error(
                    "upsert_record_failed",
                    action="upsert_record",
                    table=table_name,
                    error=str(exc),
                    record_keys=list(record.keys()),
                )

    duration_ms = int(_now_ms() - start_ms)
    logger.info(
        "upsert_completed",
        action="upsert",
        table=table_name,
        records_total=len(records),
        records_written=written,
        duration_ms=duration_ms,
    )

    return written


def query_latest(
    table_name: str,
    filters: dict | None = None,
    order_by: str = "created_at DESC",
    limit: int = 100,
    config: dict | None = None,
) -> list[dict]:
    where_clause = ""
    params = {}
    if filters:
        conditions = []
        for i, (key, value) in enumerate(filters.items()):
            param_name = f"p{i}"
            conditions.append(f"{key} = :{param_name}")
            params[param_name] = value
        where_clause = "WHERE " + " AND ".join(conditions)

    sql = f"SELECT * FROM {table_name} {where_clause} ORDER BY {order_by} LIMIT :limit"
    params["limit"] = limit

    with get_session(config) as session:
        result = session.execute(text(sql), params)
        rows = [dict(row._mapping) for row in result]

    return rows


def check_connection(config: dict | None = None) -> bool:
    try:
        with get_session(config) as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("db_connection_failed", action="db_check", error=str(exc))
        return False


def check_tables_exist(
    table_names: list[str], config: dict | None = None
) -> dict[str, bool]:
    results = {}
    for table_name in table_names:
        try:
            with get_session(config) as session:
                session.execute(text(f"SELECT 1 FROM {table_name} LIMIT 1"))
            results[table_name] = True
        except Exception:
            results[table_name] = False
    return results


def insert_records(
    table_name: str,
    records: list[dict],
    config: dict | None = None,
) -> int:
    if not records:
        return 0

    import json as _json

    start_ms = _now_ms()
    written = 0

    columns = list(records[0].keys())
    col_list = ", ".join(columns)
    placeholders = ", ".join(f":{col}" for col in columns)

    sql = f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})"
    stmt = text(sql)

    with get_session(config) as session:
        for record in records:
            try:
                prepared = {}
                for k, v in record.items():
                    if isinstance(v, (dict, list)):
                        prepared[k] = _json.dumps(v)
                    else:
                        prepared[k] = v
                nested = session.begin_nested()
                session.execute(stmt, prepared)
                nested.commit()
                written += 1
            except Exception as exc:
                nested.rollback()
                logger.error(
                    "insert_record_failed",
                    action="insert_record",
                    table=table_name,
                    error=str(exc),
                    record_keys=list(record.keys()),
                )

    duration_ms = int(_now_ms() - start_ms)
    logger.info(
        "insert_completed",
        action="insert",
        table=table_name,
        records_total=len(records),
        records_written=written,
        duration_ms=duration_ms,
    )

    return written


def _now_ms() -> float:
    return __import__("time").monotonic() * 1000
