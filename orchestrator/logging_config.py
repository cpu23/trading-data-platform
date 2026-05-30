import logging
import logging.handlers
from datetime import datetime, timezone

import structlog


def _add_correlation_id(logger, method_name, event_dict):
    if "correlation_id" not in event_dict:
        event_dict["correlation_id"] = "none"
    return event_dict


def _add_component(logger, method_name, event_dict):
    if "component" not in event_dict:
        event_dict["component"] = "unknown"
    return event_dict


def _rename_level(logger, method_name, event_dict):
    if "level" in event_dict:
        event_dict["level"] = (
            event_dict["level"].upper()
            if isinstance(event_dict["level"], str)
            else event_dict["level"]
        )
    return event_dict


def _iso_timestamp(logger, method_name, event_dict):
    event_dict["timestamp"] = datetime.now(timezone.utc).isoformat()
    return event_dict


def setup_logging(level: str = "DEBUG", correlation_id: str | None = None):
    stdlib_level = getattr(logging, level.upper(), logging.DEBUG)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        _iso_timestamp,
        _add_component,
        _add_correlation_id,
        structlog.stdlib.add_log_level,
        _rename_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(formatter)
    stdout_handler.setLevel(stdlib_level)

    handlers = [stdout_handler]

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            "/var/log/trading-data/app.log",
            maxBytes=100 * 1024 * 1024,
            backupCount=30,
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(stdlib_level)
        handlers.append(file_handler)
    except OSError:
        pass

    root_logger = logging.getLogger()
    root_logger.setLevel(stdlib_level)
    root_logger.handlers.clear()
    for h in handlers:
        root_logger.addHandler(h)

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    if correlation_id:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)


def get_logger(component: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger().bind(component=component)
