from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from logging_config import get_logger

logger = get_logger("db")


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a batch write operation (insert or upsert)."""

    attempted: int
    written: int
    failed: int
    errors: tuple[str, ...]

    @property
    def status(self) -> str:
        """``"success"``, ``"partial"``, or ``"failed"``."""
        if self.failed == 0:
            return "success"
        if self.written == 0:
            return "failed"
        return "partial"

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
    committed = False
    try:
        try:
            yield session
            session.commit()
            committed = True
        except BaseException:
            try:
                session.rollback()
            except Exception as rollback_exc:
                logger.warning(
                    "db_session_rollback_failed",
                    action="db_session_rollback",
                    error_type=type(rollback_exc).__name__,
                )
            raise
    finally:
        try:
            session.close()
        except Exception as close_exc:
            logger.warning(
                "db_session_close_failed",
                action="db_session_close",
                error_type=type(close_exc).__name__,
                transaction_committed=committed,
            )


def upsert_records(
    table_name: str,
    records: list[dict],
    conflict_columns: list[str],
    config: dict | None = None,
) -> WriteResult:
    if not records:
        return WriteResult(attempted=0, written=0, failed=0, errors=())

    schema_error = _validate_record_schemas(records)
    if schema_error is not None:
        return schema_error

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

    return _write_records(
        operation="upsert",
        table_name=table_name,
        records=records,
        stmt=text(sql),
        config=config,
    )


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
) -> WriteResult:
    if not records:
        return WriteResult(attempted=0, written=0, failed=0, errors=())

    schema_error = _validate_record_schemas(records)
    if schema_error is not None:
        return schema_error

    columns = list(records[0].keys())
    col_list = ", ".join(columns)
    placeholders = ", ".join(f":{col}" for col in columns)
    sql = f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})"

    return _write_records(
        operation="insert",
        table_name=table_name,
        records=records,
        stmt=text(sql),
        config=config,
    )


def _validate_record_schemas(records: list[dict]) -> WriteResult | None:
    """Reject heterogeneous batches without exposing keys or values."""

    canonical_keys = set(records[0])
    errors = tuple(
        f"record schema mismatch at index {index}"
        for index, record in enumerate(records[1:], start=1)
        if set(record) != canonical_keys
    )
    if not errors:
        return None
    attempted = len(records)
    return WriteResult(attempted, 0, attempted, errors)


def _prepare_records(records: list[dict]) -> list[dict]:
    import json

    return [
        {
            key: json.dumps(value) if isinstance(value, (dict, list)) else value
            for key, value in record.items()
        }
        for record in records
    ]


def _exception_type(exc: Exception) -> str:
    """Return safe diagnostic detail without exception text or record values."""

    return type(exc).__name__


def _write_records(
    *,
    operation: str,
    table_name: str,
    records: list[dict],
    stmt,
    config: dict | None,
) -> WriteResult:
    """Try one executemany, then diagnose failures in a fresh transaction."""

    start_ms = _now_ms()
    attempted = len(records)
    prepared_records = _prepare_records(records)

    try:
        with get_session(config) as session:
            session.execute(stmt, prepared_records)
    except Exception as batch_exc:
        logger.warning(
            f"{operation}_batch_failed",
            action=f"{operation}_batch",
            table=table_name,
            records_total=attempted,
            error_type=_exception_type(batch_exc),
        )
    else:
        result = WriteResult(attempted, attempted, 0, ())
        _log_write_completed(operation, table_name, result, start_ms)
        return result

    written = 0
    errors: list[str] = []
    try:
        # Never reuse the transaction invalidated by the executemany failure.
        with get_session(config) as session:
            for index, (record, prepared) in enumerate(
                zip(records, prepared_records, strict=True), start=1
            ):
                try:
                    with session.begin_nested():
                        session.execute(stmt, prepared)
                    written += 1
                except Exception as row_exc:
                    errors.append(
                        f"record {index} failed ({_exception_type(row_exc)})"
                    )
                    logger.error(
                        f"{operation}_record_failed",
                        action=f"{operation}_record",
                        table=table_name,
                        record_index=index,
                        error_type=_exception_type(row_exc),
                        record_keys=list(record.keys()),
                    )
    except Exception as fallback_exc:
        # A failed fallback transaction cannot have committed any diagnosed rows.
        result = WriteResult(
            attempted=attempted,
            written=0,
            failed=attempted,
            errors=(
                "diagnostic fallback unavailable "
                f"({_exception_type(fallback_exc)}); records not written",
            ),
        )
        logger.error(
            f"{operation}_fallback_unavailable",
            action=f"{operation}_fallback",
            table=table_name,
            records_total=attempted,
            error_type=_exception_type(fallback_exc),
        )
        _log_write_completed(operation, table_name, result, start_ms)
        return result

    result = WriteResult(
        attempted=attempted,
        written=written,
        failed=attempted - written,
        errors=tuple(errors),
    )
    _log_write_completed(operation, table_name, result, start_ms)
    return result


def _log_write_completed(
    operation: str, table_name: str, result: WriteResult, start_ms: float
) -> None:
    logger.info(
        f"{operation}_completed",
        action=operation,
        table=table_name,
        records_total=result.attempted,
        records_written=result.written,
        records_failed=result.failed,
        duration_ms=int(_now_ms() - start_ms),
    )


def _now_ms() -> float:
    return __import__("time").monotonic() * 1000
