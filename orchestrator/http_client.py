import time

import httpx

from logging_config import get_logger

logger = get_logger("http_client")

_RETRYABLE_EXCEPTIONS = (httpx.ConnectError, httpx.TimeoutException)


def _do_request(
    method: str,
    url: str,
    params: dict | None,
    headers: dict | None,
    json_body: dict | None,
    timeout: float,
    follow_redirects: bool,
) -> httpx.Response:
    with httpx.Client(timeout=timeout, follow_redirects=follow_redirects) as client:
        return client.request(
            method=method.upper(),
            url=url,
            params=params,
            headers=headers,
            json=json_body,
        )


def make_request(
    method: str,
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    json_body: dict | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    correlation_id: str | None = None,
    follow_redirects: bool = False,
) -> httpx.Response:
    """Make an HTTP request with a configurable maximum number of attempts.

    ``max_retries`` historically represented total attempts in this project.
    Keep that behavior so callers passing ``1`` still get one request, while
    ensuring the argument is no longer ignored by a fixed retry decorator.
    """
    max_attempts = max(1, int(max_retries))
    start = time.monotonic()
    attempts = 0

    while attempts < max_attempts:
        attempts += 1
        try:
            response = _do_request(
                method,
                url,
                params,
                headers,
                json_body,
                timeout,
                follow_redirects,
            )
            break
        except _RETRYABLE_EXCEPTIONS as exc:
            if attempts >= max_attempts:
                duration_ms = int((time.monotonic() - start) * 1000)
                metadata = {
                    "attempts": attempts,
                    "retries": attempts - 1,
                    "duration_ms": duration_ms,
                    "max_attempts": max_attempts,
                }
                setattr(exc, "request_metadata", metadata)
                logger.error(
                    "http_request_failed",
                    action="http_request",
                    method=method.upper(),
                    url=url,
                    error=str(exc),
                    attempts=attempts,
                    duration_ms=duration_ms,
                    correlation_id=correlation_id or "none",
                )
                raise

            wait_seconds = min(2 ** (attempts - 1), 10)
            logger.warning(
                "http_request_retrying",
                action="http_request",
                method=method.upper(),
                url=url,
                error=str(exc),
                attempt=attempts,
                max_attempts=max_attempts,
                wait_seconds=wait_seconds,
                correlation_id=correlation_id or "none",
            )
            time.sleep(wait_seconds)

    duration_ms = int((time.monotonic() - start) * 1000)
    response_size = len(response.content) if response.content else 0
    response.extensions["request_metadata"] = {
        "attempts": attempts,
        "retries": attempts - 1,
        "duration_ms": duration_ms,
        "max_attempts": max_attempts,
    }

    logger.info(
        "http_request_completed",
        action="http_request",
        method=method.upper(),
        url=url,
        status_code=response.status_code,
        attempts=attempts,
        duration_ms=duration_ms,
        response_size=response_size,
        correlation_id=correlation_id or "none",
    )

    return response
