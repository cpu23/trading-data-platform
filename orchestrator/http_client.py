import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from logging_config import get_logger

logger = get_logger("http_client")

DEFAULT_SOURCE_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_DELAY_SECONDS = 60.0
_MAX_BACKOFF_SECONDS = 10.0
_shared_client: httpx.Client | None = None
_shared_client_lock = threading.Lock()


def get_shared_client() -> httpx.Client:
    """Return the process-wide connection-pooled client."""
    global _shared_client
    with _shared_client_lock:
        if _shared_client is None:
            _shared_client = httpx.Client()
        return _shared_client


def close_shared_client() -> None:
    """Close and clear the shared client exactly once."""
    global _shared_client
    with _shared_client_lock:
        client = _shared_client
        _shared_client = None
    if client is not None:
        client.close()


def _backoff_seconds(attempt: int) -> float:
    return min(float(2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS)


def _retry_delay(
    response: httpx.Response | None,
    attempt: int,
    wall_clock: Callable[[], datetime],
) -> float:
    fallback = _backoff_seconds(attempt)
    if response is None or response.status_code != 429:
        return fallback

    raw_value = response.headers.get("Retry-After")
    if raw_value is None:
        return fallback
    try:
        seconds = float(raw_value.strip())
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(raw_value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            now = wall_clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            seconds = (retry_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return fallback
        return min(max(seconds, 0.0), _MAX_RETRY_DELAY_SECONDS)
    if not math.isfinite(seconds) or seconds < 0:
        return fallback
    return min(seconds, _MAX_RETRY_DELAY_SECONDS)


def _duration_ms(clock: Callable[[], float], started_at: float) -> int:
    return max(0, int((clock() - started_at) * 1000))


def make_request(
    method: str,
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    json_body: dict | None = None,
    timeout: httpx.Timeout | float = DEFAULT_SOURCE_TIMEOUT,
    max_retries: int = 3,
    correlation_id: str | None = None,
    follow_redirects: bool = False,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> httpx.Response:
    """Make an HTTP request, with ``max_retries`` interpreted as total attempts."""
    if max_retries < 1:
        raise ValueError(
            "max_retries must be at least 1 (it is the total attempt count)"
        )

    started_at = clock()
    request_method = method.upper()
    request_client = client or get_shared_client()

    try:
        for attempt in range(1, max_retries + 1):
            response = None
            category = "network"
            try:
                response = request_client.request(
                    method=request_method,
                    url=url,
                    params=params,
                    headers=headers,
                    json=json_body,
                    timeout=timeout,
                    follow_redirects=follow_redirects,
                )
            except httpx.TransportError as exc:
                if attempt == max_retries:
                    logger.error(
                        "http_request_failed",
                        action="http_request",
                        method=request_method,
                        attempt=attempt,
                        max_attempts=max_retries,
                        category=category,
                        error_type=type(exc).__name__,
                        total_duration_ms=_duration_ms(clock, started_at),
                        correlation_id=correlation_id or "none",
                    )
                    raise
            else:
                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    response_size = len(response.content) if response.content else 0
                    response.extensions["request_metadata"] = {
                        "attempts": attempt,
                        "max_attempts": max_retries,
                        "duration_ms": _duration_ms(clock, started_at),
                    }
                    logger.info(
                        "http_request_completed",
                        action="http_request",
                        method=request_method,
                        attempt=attempt,
                        max_attempts=max_retries,
                        status_code=response.status_code,
                        category="success"
                        if response.is_success
                        else "non_transient_status",
                        total_duration_ms=_duration_ms(clock, started_at),
                        response_size=response_size,
                        correlation_id=correlation_id or "none",
                    )
                    return response

                category = "http_status"
                if attempt == max_retries:
                    logger.error(
                        "http_request_failed",
                        action="http_request",
                        method=request_method,
                        attempt=attempt,
                        max_attempts=max_retries,
                        status_code=response.status_code,
                        category=category,
                        total_duration_ms=_duration_ms(clock, started_at),
                        correlation_id=correlation_id or "none",
                    )
                    response.raise_for_status()

            delay = _retry_delay(response, attempt, wall_clock)
            retry_fields = {
                "action": "http_request",
                "method": request_method,
                "attempt": attempt,
                "max_attempts": max_retries,
                "category": category,
                "delay_seconds": delay,
                "total_duration_ms": _duration_ms(clock, started_at),
                "correlation_id": correlation_id or "none",
            }
            if response is not None:
                retry_fields["status_code"] = response.status_code
            logger.warning("http_request_retrying", **retry_fields)
            sleep(delay)
    finally:
        pass
    raise RuntimeError("HTTP request loop exited unexpectedly")
