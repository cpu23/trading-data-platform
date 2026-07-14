import logging
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import structlog

_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "key",
    "authorization",
    "proxy_authorization",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "cookie",
    "set_cookie",
}
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_AUTH_RE = re.compile(
    r"(?i)\b(authorization|proxy[-_ ]authorization)\s*[:=]\s*(?:(?:bearer|basic)\s+)?[^\s,;]+"
)
_SCHEME_CREDENTIAL_RE = re.compile(r"(?i)\b(bearer|basic)\s+[^\s,;]+")
_SENSITIVE_KEY_PATTERN = "|".join(
    re.escape(key).replace("_", "[-_]")
    for key in sorted(_SENSITIVE_KEYS, key=len, reverse=True)
)
_NAMED_CREDENTIAL_RE = re.compile(
    rf"(?i)(?<![\w-])(?P<key_quote>[\"']?)(?P<key>{_SENSITIVE_KEY_PATTERN})"
    rf"(?P=key_quote)(?P<separator>\s*[:=]\s*)"
    r'(?:(?:"(?P<double_value>(?:\\.|[^"\\])*)")|'
    r"(?:'(?P<single_value>(?:\\.|[^'\\])*)')|"
    r"(?P<unquoted_value>\[REDACTED\]|[^\s,;&#}\])\"']+))"
)
_DEPENDENCY_LOGGERS = ("httpx", "httpcore", "sqlalchemy", "sqlalchemy.engine")


def _normalized_key(key: object) -> str | None:
    if not isinstance(key, str):
        return None
    return key.strip().lower().replace("-", "_")


def _redact_url(url: str) -> str:
    trailing = ""
    while url and url[-1] in ".,;!)]}":
        trailing = url[-1] + trailing
        url = url[:-1]
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if "@" in netloc:
            _userinfo, _separator, hostinfo = netloc.rpartition("@")
            netloc = f"{_REDACTED}@{hostinfo}"
        query_parts = []
        for item in parts.query.split("&"):
            name, separator, value = item.partition("=")
            if _normalized_key(unquote_plus(name)) in _SENSITIVE_KEYS:
                query_parts.append(f"{name}{separator}{_REDACTED}" if separator else f"{name}={_REDACTED}")
            else:
                query_parts.append(item)
        return urlunsplit((parts.scheme, netloc, parts.path, "&".join(query_parts), parts.fragment)) + trailing
    except Exception:
        # Never return a raw query string when parsing fails.
        prefix, marker, _query = url.partition("?")
        return (prefix + (f"?{_REDACTED}" if marker else "")) + trailing


def _sanitize_string(value: str) -> str:
    value = _URL_RE.sub(lambda match: _redact_url(match.group(0)), value)
    value = _AUTH_RE.sub(lambda match: f"{match.group(1)}: {_REDACTED}", value)
    value = _SCHEME_CREDENTIAL_RE.sub(lambda match: f"{match.group(1)} {_REDACTED}", value)

    def redact_named(match: re.Match[str]) -> str:
        if match.group("double_value") is not None:
            quote = '"'
        elif match.group("single_value") is not None:
            quote = "'"
        else:
            quote = ""
        return (
            f'{match.group("key_quote")}{match.group("key")}{match.group("key_quote")}'
            f'{match.group("separator")}{quote}{_REDACTED}{quote}'
        )

    return _NAMED_CREDENTIAL_RE.sub(redact_named, value)


def _safe_key(key: object) -> object:
    if key is None or isinstance(key, (str, int, float, bool)):
        return key
    return f"<unserializable {type(key).__name__}>"


def _sanitize(value: object, *, preserve_exception: bool = False) -> object:
    if preserve_exception:
        return value
    if isinstance(value, str):
        return _sanitize_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        try:
            sanitized = {}
            for key, item in value.items():
                normalized = _normalized_key(key)
                safe_key = _safe_key(key)
                if normalized in _SENSITIVE_KEYS:
                    sanitized[safe_key] = _REDACTED
                else:
                    sanitized[safe_key] = _sanitize(
                        item, preserve_exception=normalized == "exc_info"
                    )
            return sanitized
        except Exception:
            return f"<unserializable {type(value).__name__}>"
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return [_sanitize(item) for item in value]
    return f"<unserializable {type(value).__name__}>"


def redact_credentials(logger, method_name, event_dict):
    """Return a recursively sanitized copy of a structlog event dictionary."""
    sanitized = _sanitize(event_dict)
    return sanitized if isinstance(sanitized, dict) else {"event": sanitized}


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


def setup_logging(level: str = "INFO", correlation_id: str | None = None):
    stdlib_level = getattr(logging, level.upper(), logging.INFO)
    dependency_level = logging.DEBUG if stdlib_level <= logging.DEBUG else logging.WARNING

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        redact_credentials,
        _iso_timestamp,
        _add_component,
        _add_correlation_id,
        structlog.stdlib.add_log_level,
        _rename_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_credentials,
    ]

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            redact_credentials,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.setLevel(stdlib_level)

    root_logger = logging.getLogger()
    root_logger.setLevel(stdlib_level)
    root_logger.handlers.clear()
    root_logger.addHandler(stdout_handler)

    for logger_name in _DEPENDENCY_LOGGERS:
        logging.getLogger(logger_name).setLevel(dependency_level)

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
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
