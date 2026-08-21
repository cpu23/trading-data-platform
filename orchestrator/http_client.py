import hashlib
import math
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from contracts.outbound_security import (
    parse_origin,
    resolve_redirect_url,
)
from contracts.outbound_transport import PublicOnlyHTTPTransport
from logging_config import get_logger

logger = get_logger("http_client")

DEFAULT_SOURCE_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_DELAY_SECONDS = 60.0
_BACKOFF_BASE_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 10.0
# Total-operation budget applied when the caller does not override it: every
# request, including its retries and sleeps, completes within this window.
DEFAULT_DEADLINE_SECONDS = 60.0
# Methods whose replay cannot create duplicate side effects on the server.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE"})
_IDEMPOTENCY_HEADER = "Idempotency-Key"
# Headers that carry credentials but are NOT stripped by httpx on cross-origin
# redirects (httpx only strips ``Authorization``).  Following a redirect with
# one of these set would replay the credential to a new origin, so
# ``make_request`` refuses ``follow_redirects=True`` when one is present.
_REDIRECT_CREDENTIAL_HEADERS = frozenset(
    {
        "x-api-key",
        "api-key",
        "apikey",
        "x-auth-token",
        "x-access-token",
        "proxy-authorization",
        "cookie",
    }
)
# Redirect hops followed when a response byte cap is set. httpx's native
# follow_redirects materializes every intermediate redirect body before any
# cap can be enforced, so capped requests follow redirects manually, one
# bounded hop at a time, and give up after this many hops.
MAX_REDIRECT_HOPS = 5
_shared_clients: dict[int, httpx.Client] = {}
_shared_client_lock = threading.Lock()


class RequestDeadlineExceeded(httpx.TimeoutException):
    """Raised when the total request budget (attempts plus retry sleeps) runs out."""


class ResponseBodyTooLarge(httpx.RequestError):
    """Raised while streaming when a response exceeds its caller-owned cap."""


def get_shared_client() -> httpx.Client:
    """Return this caller thread's connection-pooled outbound client.

    Full-cycle collectors execute concurrently on separate worker threads.
    Keeping one pool per caller thread lets a hard deadline abort that
    thread's stuck transport without closing unrelated providers' in-flight
    sockets. Every pool uses the resolve-and-pin
    :class:`PublicOnlyHTTPTransport`, so each send re-resolves the host,
    requires every DNS answer to be public, pins the connection to a validated
    address, and re-validates every redirect hop.
    """
    thread_id = threading.get_ident()
    with _shared_client_lock:
        client = _shared_clients.get(thread_id)
        if client is None:
            client = httpx.Client(
                transport=PublicOnlyHTTPTransport(),
                follow_redirects=False,
            )
            _shared_clients[thread_id] = client
        return client


def close_shared_client() -> None:
    """Close and clear every caller-thread pool at role shutdown."""
    with _shared_client_lock:
        clients = list(
            {id(client): client for client in _shared_clients.values()}.values()
        )
        _shared_clients.clear()
    for client in clients:
        client.close()


def stable_idempotency_key(
    method: str, url: str, body: bytes | None, *, scope: str = ""
) -> str:
    """Deterministic key for ONE logical operation so retries never diverge.

    ``scope`` must identify the logical operation (e.g. correlation id or a
    per-operation nonce): two legitimate operations that happen to share
    method/URL/body must NOT collide, while every transport retry of the
    same operation reuses the same key.
    """
    digest = hashlib.sha256()
    digest.update(str(method).upper().encode("ascii"))
    digest.update(b"\x00")
    digest.update(str(url).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(body if body is not None else b"")
    digest.update(b"\x00")
    digest.update(str(scope).encode("utf-8"))
    return digest.hexdigest()


def _with_idempotency_key(
    headers: dict | None, idempotency_key: str | None
) -> dict | None:
    if not idempotency_key:
        return headers
    merged = dict(headers or {})
    merged.setdefault(_IDEMPOTENCY_HEADER, str(idempotency_key))
    return merged


def _has_idempotency_header(headers: dict | None) -> bool:
    if not headers:
        return False
    return any(str(key).lower() == _IDEMPOTENCY_HEADER.lower() for key in headers)


def _reject_credentialed_redirects(url: str, headers: dict | None) -> None:
    """Fail closed when a request that follows redirects carries credentials.

    httpx strips ``Authorization`` on cross-origin redirects, so header-based
    auth stays with its origin.  URL-embedded userinfo and non-standard
    credential headers are NOT stripped by httpx and would be replayed to
    (or surfaced for) a different origin; refuse to follow redirects for
    those requests instead.
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        parts = None
    if parts is not None and "@" in (parts.netloc or ""):
        raise ValueError(
            "refusing to follow redirects for a request whose URL embeds "
            "credentials (userinfo)"
        )
    for key, _value in (headers or {}).items():
        if str(key).strip().lower().replace("_", "-") in _REDIRECT_CREDENTIAL_HEADERS:
            raise ValueError(
                "refusing to follow redirects for a request carrying a "
                f"credential header ({key}) that httpx would not strip"
            )


def _backoff_cap(attempt: int) -> float:
    return min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS)


def _retry_delay(
    response: httpx.Response | None,
    attempt: int,
    wall_clock: Callable[[], datetime],
) -> float:
    """Bounded full-jitter delay for one retry.

    429 responses honor ``Retry-After`` (capped) when present; otherwise the
    delay is sampled uniformly from ``[0, base * 2 ** (attempt - 1)]``
    (full jitter, per AWS/Google guidance) so bursts do not synchronize.
    """
    fallback_window = _backoff_cap(attempt)
    if response is None or response.status_code != 429:
        return random.uniform(0.0, fallback_window)

    raw_value = response.headers.get("Retry-After")
    if raw_value is None:
        return random.uniform(0.0, fallback_window)
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
            return random.uniform(0.0, fallback_window)
        return min(max(seconds, 0.0), _MAX_RETRY_DELAY_SECONDS)
    if not math.isfinite(seconds) or seconds < 0:
        return random.uniform(0.0, fallback_window)
    return min(seconds, _MAX_RETRY_DELAY_SECONDS)


def _duration_ms(clock: Callable[[], float], started_at: float) -> int:
    return max(0, int((clock() - started_at) * 1000))


def _timeout_budget(timeout: httpx.Timeout | float) -> float:
    """Upper-bound estimate of one attempt's wall-clock timeout budget."""
    if isinstance(timeout, httpx.Timeout):
        phases = (timeout.connect, timeout.read, timeout.write, timeout.pool)
        finite = [value for value in phases if value is not None]
        return max(finite) if finite else 0.0
    return float(timeout)


def _close_timed_out_client(client) -> None:
    """Abort only the registered caller-thread pool that hit its deadline."""
    with _shared_client_lock:
        owners = [
            thread_id
            for thread_id, registered in _shared_clients.items()
            if registered is client
        ]
        for thread_id in owners:
            del _shared_clients[thread_id]
    # An injected caller-owned client may be shared by unrelated operations;
    # never close it behind the caller's back. Registered clients are isolated
    # per caller thread and are therefore safe to abort.
    if owners:
        client.close()


def _redirect_method(method: str, status_code: int) -> str:
    """RFC/browser-compatible method rewrite for one redirect hop."""
    if status_code == 303 and method != "HEAD":
        return "GET"
    if status_code == 302 and method != "HEAD":
        return "GET"
    if status_code == 301 and method == "POST":
        return "GET"
    return method


def _redirect_target(current: str, location: str) -> str:
    """Resolve one redirect hop under the outbound policy.

    The target must be a well-formed http(s) URL (https by default, no
    downgrade, no embedded credentials). DNS and public-routability are
    re-validated at send time by the shared resolve-and-pin transport, which
    every hop re-enters.
    """
    return resolve_redirect_url(current, location)


def _same_origin(current: str, target: str) -> bool:
    current_origin = parse_origin(current)
    target_origin = parse_origin(target)
    return (current_origin.scheme, current_origin.host, current_origin.port) == (
        target_origin.scheme,
        target_origin.host,
        target_origin.port,
    )


def _strip_credentials_for_hop(kwargs: dict) -> None:
    """Drop auth (argument or Authorization header) for a cross-origin hop.

    httpx strips ``Authorization`` when a redirect leaves the origin; the
    manual loop reproduces that so a credential is never forwarded across
    origins. The ``auth`` argument must be dropped too: httpx would otherwise
    re-derive and re-attach the header on the new origin.
    """
    kwargs.pop("auth", None)
    headers = kwargs.get("headers")
    if headers:
        kwargs["headers"] = {
            key: value
            for key, value in headers.items()
            if str(key).lower() != "authorization"
        }


def _stream_bounded_response(
    client,
    *,
    max_response_bytes: int,
    **kwargs,
) -> httpx.Response:
    follow_redirects = bool(kwargs.pop("follow_redirects", False))
    max_redirects = int(kwargs.pop("max_redirects", MAX_REDIRECT_HOPS))
    history: list[httpx.Response] = []
    method = str(kwargs.get("method", "GET")).upper()
    for _hop in range(max_redirects + 1):
        with client.stream(follow_redirects=False, **kwargs) as response:
            declared = response.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > max_response_bytes:
                        raise ResponseBodyTooLarge(
                            f"response exceeds {max_response_bytes} bytes",
                            request=response.request,
                        )
                except ValueError:
                    pass
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > max_response_bytes:
                    raise ResponseBodyTooLarge(
                        f"response exceeds {max_response_bytes} bytes",
                        request=response.request,
                    )
            hop = httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(body),
                request=response.request,
                extensions=dict(response.extensions),
                history=list(history),
            )
        if not (follow_redirects and hop.has_redirect_location):
            return hop
        if method not in _IDEMPOTENT_METHODS:
            # A redirect must never silently replay a non-idempotent request
            # (307/308 would re-send its body); return the bounded redirect
            # response and let the caller decide.
            return hop
        location = hop.headers.get("location")
        if not location or not str(location).strip():
            return hop
        current = str(hop.request.url)
        target = _redirect_target(current, str(location))
        # Defense in depth: the same non-strippable-credential policy that
        # make_request enforces up front applies to every hop.
        _reject_credentialed_redirects(target, kwargs.get("headers"))
        if not _same_origin(current, target):
            _strip_credentials_for_hop(kwargs)
        history.append(hop)
        next_method = _redirect_method(method, hop.status_code)
        kwargs["url"] = target
        kwargs["method"] = next_method
        if next_method != method:
            kwargs.pop("json", None)
        # Redirect targets already carry their own query string; never
        # re-apply the original request params.
        kwargs.pop("params", None)
        method = next_method
    raise httpx.TooManyRedirects(
        f"exceeded {max_redirects} redirect hops",
        request=httpx.Request(
            method,
            str(kwargs.get("url", "")),
            headers=kwargs.get("headers"),
        ),
    )


def _request_with_deadline(client, remaining: float, **kwargs) -> httpx.Response:
    """Run one synchronous send behind a hard wall-clock cancellation point."""
    max_response_bytes = kwargs.pop("max_response_bytes", None)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bounded-http")
    if max_response_bytes is None:
        future = executor.submit(client.request, **kwargs)
    else:
        future = executor.submit(
            _stream_bounded_response,
            client,
            max_response_bytes=max_response_bytes,
            **kwargs,
        )
    try:
        return future.result(timeout=remaining)
    except FutureTimeoutError as exc:
        _close_timed_out_client(client)
        future.cancel()
        request = httpx.Request(
            kwargs["method"],
            kwargs["url"],
            headers=kwargs.get("headers"),
        )
        raise RequestDeadlineExceeded(
            "HTTP request total deadline exceeded",
            request=request,
        ) from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


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
    auth: httpx.Auth | tuple[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    idempotency_key: str | None = None,
    deadline_seconds: float | None = None,
    max_response_bytes: int | None = None,
) -> httpx.Response:
    """Make an HTTP request, with ``max_retries`` interpreted as total attempts.

    Retry capability is determined before the first send: idempotent methods
    (GET/HEAD/PUT/DELETE/OPTIONS/TRACE) retry freely; POST/PATCH retry only
    when an ``idempotency_key`` argument or ``Idempotency-Key`` header is
    supplied. Non-idempotent requests are never replayed, so a POST cannot be
    duplicated by a transport error or 5xx response.

    Retries use bounded full-jitter delays; ``Retry-After`` on 429 responses
    is honored (capped). A total-operation deadline bounds the entire call
    including retry sleeps: ``deadline_seconds`` overrides the bounded
    default (``DEFAULT_DEADLINE_SECONDS``), and each per-attempt timeout is
    clamped so a single attempt cannot run past the remaining budget.
    Exceeding the deadline raises :class:`RequestDeadlineExceeded`.

    ``max_response_bytes`` bounds the response body incrementally while it
    streams: an oversized declared ``Content-Length`` is rejected before any
    bytes are read, actual bytes are counted per chunk (so missing or
    false-small ``Content-Length`` and chunked bodies are covered), and the
    stream is closed as soon as the cap is exceeded by raising
    :class:`ResponseBodyTooLarge`.

    Credentialed redirects fail closed: requests that follow redirects must
    not embed credentials in the URL (userinfo) or carry credential headers
    that httpx would replay to a different origin (anything other than
    ``Authorization``, which httpx strips on cross-origin redirects).

    When ``follow_redirects`` is combined with ``max_response_bytes``,
    redirects are followed manually instead of by httpx: every hop —
    including intermediate redirect responses — is streamed under the byte
    cap, the hop count is bounded by :data:`MAX_REDIRECT_HOPS`, each target
    is shape-validated (https-only, no downgrade) and re-validated against
    DNS and pinned by the shared public-only transport at send time, and
    ``Authorization``/``auth`` is stripped before any cross-origin hop so a
    credential never leaves its origin. Non-idempotent requests are never
    replayed by redirect following.
    """
    if max_retries < 1:
        raise ValueError(
            "max_retries must be at least 1 (it is the total attempt count)"
        )
    if deadline_seconds is not None and deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be positive")
    if max_response_bytes is not None and max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")

    started_at = clock()
    request_method = method.upper()
    if follow_redirects:
        _reject_credentialed_redirects(url, headers)
    total_budget = (
        float(deadline_seconds)
        if deadline_seconds is not None
        else DEFAULT_DEADLINE_SECONDS
    )
    request_headers = _with_idempotency_key(headers, idempotency_key)
    idempotent = (
        request_method in _IDEMPOTENT_METHODS
        or bool(idempotency_key)
        or _has_idempotency_header(request_headers)
    )
    total_attempts = max_retries if idempotent else 1
    if max_retries > 1 and not idempotent:
        logger.warning(
            "http_request_retries_disabled_non_idempotent",
            action="http_request",
            method=request_method,
            requested_attempts=max_retries,
            attempts=1,
            correlation_id=correlation_id or "none",
        )
    request_client = client or get_shared_client()

    for attempt in range(1, total_attempts + 1):
        elapsed = clock() - started_at
        remaining = total_budget - elapsed
        if remaining <= 0:
            raise RequestDeadlineExceeded(
                "HTTP request total deadline exceeded",
                request=httpx.Request(request_method, url, headers=request_headers),
            )
        attempt_timeout = timeout
        if _timeout_budget(timeout) > remaining:
            attempt_timeout = httpx.Timeout(remaining)
        response = None
        category = "network"
        try:
            response = _request_with_deadline(
                request_client,
                remaining,
                method=request_method,
                url=url,
                params=params,
                headers=request_headers,
                auth=auth,
                json=json_body,
                timeout=attempt_timeout,
                follow_redirects=follow_redirects,
                max_response_bytes=max_response_bytes,
            )
            if clock() - started_at >= total_budget:
                response.close()
                raise RequestDeadlineExceeded(
                    "HTTP request total deadline exceeded",
                    request=httpx.Request(
                        request_method,
                        url,
                        headers=request_headers,
                    ),
                )
        except RequestDeadlineExceeded:
            raise
        except httpx.TransportError as exc:
            if attempt == total_attempts:
                logger.error(
                    "http_request_failed",
                    action="http_request",
                    method=request_method,
                    attempt=attempt,
                    max_attempts=total_attempts,
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
                    "max_attempts": total_attempts,
                    "duration_ms": _duration_ms(clock, started_at),
                }
                logger.info(
                    "http_request_completed",
                    action="http_request",
                    method=request_method,
                    attempt=attempt,
                    max_attempts=total_attempts,
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
            if attempt == total_attempts:
                logger.error(
                    "http_request_failed",
                    action="http_request",
                    method=request_method,
                    attempt=attempt,
                    max_attempts=total_attempts,
                    status_code=response.status_code,
                    category=category,
                    total_duration_ms=_duration_ms(clock, started_at),
                    correlation_id=correlation_id or "none",
                )
                response.raise_for_status()

        delay = _retry_delay(response, attempt, wall_clock)
        remaining = total_budget - (clock() - started_at)
        if remaining <= 0:
            raise RequestDeadlineExceeded(
                "HTTP request total deadline exceeded",
                request=httpx.Request(request_method, url, headers=request_headers),
            )
        delay = min(delay, remaining)
        retry_fields = {
            "action": "http_request",
            "method": request_method,
            "attempt": attempt,
            "max_attempts": total_attempts,
            "category": category,
            "delay_seconds": delay,
            "total_duration_ms": _duration_ms(clock, started_at),
            "correlation_id": correlation_id or "none",
        }
        if response is not None:
            retry_fields["status_code"] = response.status_code
        logger.warning("http_request_retrying", **retry_fields)
        sleep(delay)
    raise RuntimeError("HTTP request loop exited unexpectedly")
