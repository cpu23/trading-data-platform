import time

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from logging_config import get_logger

logger = get_logger("http_client")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    reraise=True,
)
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
        response = client.request(
            method=method.upper(),
            url=url,
            params=params,
            headers=headers,
            json=json_body,
        )
    return response


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
    start_ms = time.monotonic() * 1000

    try:
        response = _do_request(
            method, url, params, headers, json_body, timeout, follow_redirects
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        duration_ms = int(time.monotonic() * 1000 - start_ms)
        logger.error(
            "http_request_failed",
            action="http_request",
            method=method.upper(),
            url=url,
            error=str(exc),
            duration_ms=duration_ms,
            correlation_id=correlation_id or "none",
        )
        raise

    duration_ms = int(time.monotonic() * 1000 - start_ms)
    response_size = len(response.content) if response.content else 0

    logger.info(
        "http_request_completed",
        action="http_request",
        method=method.upper(),
        url=url,
        status_code=response.status_code,
        duration_ms=duration_ms,
        response_size=response_size,
        correlation_id=correlation_id or "none",
    )

    return response
