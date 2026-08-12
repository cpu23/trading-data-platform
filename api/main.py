import hmac
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from auth import (
    ACTIVATION_FILE,
    AUTH_FILE,
    CSRF_COOKIE,
    CSRF_TOKEN_EXEMPT_PATHS,
    OPERATOR_FILE,
    STATE_DIR,
    cookie_secure,
    decode_session_cookie,
    deployment_mode,
    expected_origin,
    load_previous_session_secret,
    load_session_secret,
    migrate_legacy_state,
    mint_csrf_token,
    normalize_origin,
    session_max_age_seconds,
    setup_complete,
    sign_session_cookie,
    validate_cookie_security,
    validate_host_security,
    validate_operator_credentials,
    validate_signing_keys,
    verify_credentials,
    verify_csrf_token,
)
from config import config_version, load_config, orchestrator_url
from db import check_connection
from logging_config import setup_logging
from routes.json import router as json_router
from routes.stream import stream_router
from routes.views import router as views_router

_JSON_BODY_LIMIT = 1024 * 1024
_JSON_PATH_LIMITS = {
    "/api/login": 4 * 1024,
    "/api/setup/activate": 128 * 1024,
    "/api/setup/test-connection": 16 * 1024,
}
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_STREAMING_BODY_PATHS = frozenset({"/api/investment/documents"})


class RequestBodyLimitMiddleware:
    """Reject declared and chunked request bodies before framework buffering."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") not in _BODY_METHODS
            or scope.get("path") in _STREAMING_BODY_PATHS
        ):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        # MIME type is attacker-controlled. Apply the limit before content-type
        # validation so text/plain cannot make FastAPI buffer an unbounded body.

        limit = _JSON_PATH_LIMITS.get(scope.get("path", ""), _JSON_BODY_LIMIT)
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                response = JSONResponse(
                    status_code=400, content={"detail": "Invalid Content-Length"}
                )
                await response(scope, receive, send)
                return
            if declared_size < 0:
                response = JSONResponse(
                    status_code=400, content={"detail": "Invalid Content-Length"}
                )
                await response(scope, receive, send)
                return
            if declared_size > limit:
                response = JSONResponse(
                    status_code=413, content={"detail": "Request body too large"}
                )
                await response(scope, receive, send)
                return

        buffered = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(buffered) + len(chunk) > limit:
                response = JSONResponse(
                    status_code=413, content={"detail": "Request body too large"}
                )
                await response(scope, receive, send)
                return
            buffered.extend(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {
                "type": "http.request",
                "body": bytes(buffered),
                "more_body": False,
            }

        await self.app(scope, replay, send)


def _origin_matches(request: Request, supplied_origin: str) -> bool:
    """True when a browser-supplied Origin/Referer matches the canonical origin."""
    try:
        parsed = urlsplit(supplied_origin)
        return (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and normalize_origin(parsed) == expected_origin(request)
        )
    except ValueError:
        return False


def create_app(
    orchestrator_client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> FastAPI:
    validate_signing_keys()
    allowed_hosts = validate_host_security()
    validate_operator_credentials()
    validate_cookie_security()
    migrate_legacy_state()
    config = load_config()
    log_level = config.get("logging", {}).get("level", "INFO")
    setup_logging(level=log_level)

    client_factory = orchestrator_client_factory or httpx.AsyncClient

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.orchestrator_client = client_factory(
            timeout=httpx.Timeout(connect=2.0, read=10.0, write=10.0, pool=2.0),
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
        )
        try:
            yield
        finally:
            await app.state.orchestrator_client.aclose()

    app = FastAPI(
        title="Trading Data API",
        version="0.1.0",
        lifespan=lifespan,
        dependencies=[Depends(verify_credentials)],
    )

    session_secrets = [load_session_secret()]
    previous_secret = load_previous_session_secret()
    if previous_secret is not None:
        session_secrets.append(previous_secret)
    session_max_age = session_max_age_seconds()

    # Registration order matters: signed_session must wrap csrf_contract so
    # session cookies are decoded before any authentication or CSRF decision.
    @app.middleware("http")
    async def csrf_contract(request: Request, call_next):
        path = request.url.path
        token_exempt = path in CSRF_TOKEN_EXEMPT_PATHS
        unsafe = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        session_auth = bool(request.scope.get("session", {}).get("authenticated"))
        header_auth = bool(request.headers.get("authorization"))
        has_auth = header_auth or session_auth
        if unsafe and not has_auth and not token_exempt:
            # No credentials: the auth dependency rejects these requests, so
            # CSRF processing adds nothing and would mint needless tokens.
            return await call_next(request)

        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        token = cookie_token if verify_csrf_token(cookie_token) else mint_csrf_token()
        request.state.csrf_token = token

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        browser_signal = bool(
            origin
            or referer
            or request.cookies.get(CSRF_COOKIE)
            or request.headers.get("sec-fetch-site")
        )

        if unsafe:
            # Browser signals are computed for every method so safe browser
            # requests may receive a CSRF cookie while stateless machine
            # clients remain cookie-free.
            # The machine exemption is only for Authorization-authenticated API
            # clients: JSON body, no browser signals, and no session. A
            # session-authenticated request is a browser flow and is never
            # exempt, even when it carries no Origin header.
            machine_json = (
                header_auth
                and not session_auth
                and not browser_signal
                and request.headers.get("content-type", "").split(";", 1)[0].lower()
                == "application/json"
            )
            if not machine_json:
                supplied_origin = origin or referer
                if session_auth and not token_exempt:
                    # Session mutations require a canonical Origin plus a valid
                    # signed token; no machine exemption.
                    if not supplied_origin or not _origin_matches(
                        request, supplied_origin
                    ):
                        return JSONResponse(
                            status_code=403,
                            content={"detail": "CSRF validation failed"},
                        )
                elif supplied_origin and not _origin_matches(request, supplied_origin):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF validation failed"},
                    )
                if not token_exempt:
                    supplied = request.headers.get("x-csrf-token", "")
                    cookie_value = request.cookies.get(CSRF_COOKIE, "")
                    # Coherent double submit: the cookie must hold a signed
                    # token and the header must carry the exact same value.
                    if (
                        not cookie_value
                        or not verify_csrf_token(cookie_value)
                        or not verify_csrf_token(supplied)
                        or not hmac.compare_digest(supplied, cookie_value)
                    ):
                        return JSONResponse(
                            status_code=403,
                            content={"detail": "CSRF validation failed"},
                        )
        stateless_machine = header_auth and not session_auth and not browser_signal
        response = await call_next(request)
        if (
            token
            and response.status_code < 400
            and path not in {"/static", "/api/quotes/stream", "/stream"}
            and (request.method == "GET" or token_exempt)
            and not stateless_machine
        ):
            # Bootstrap responses (login/activate) hand the exact same token to
            # the caller via the body and the cookie, so the next mutation can
            # satisfy double submit immediately. Stateless Authorization-only
            # calls never receive the cookie so their machine exemption cannot
            # be poisoned by a cookie jar.
            response.set_cookie(
                CSRF_COOKIE,
                token,
                secure=cookie_secure(),
                httponly=False,
                samesite="strict",
                path="/",
            )
        return response

    @app.middleware("http")
    async def signed_session(request: Request, call_next):
        session = {}
        cookie = request.cookies.get("market_session")
        if cookie:
            for secret in session_secrets:
                decoded = decode_session_cookie(cookie, secret=secret)
                if decoded:
                    session = decoded
                    break
            issued_at = int(session.get("issued_at", 0) or 0)
            if session.get("authenticated") and (
                not issued_at
                or int(datetime.now(UTC).timestamp()) - issued_at > session_max_age
            ):
                session = {}
        request.scope["session"] = session
        before = dict(session)
        response = await call_next(request)
        if before and not session:
            response.delete_cookie("market_session", path="/")
        elif session != before:
            encoded = sign_session_cookie(session, secret=session_secrets[0])
            response.set_cookie(
                "market_session",
                encoded,
                httponly=True,
                samesite="strict",
                secure=cookie_secure(),
                max_age=session_max_age,
                path="/",
            )
        return response

    app.add_middleware(RequestBodyLimitMiddleware)
    if allowed_hosts is not None:
        # Registered last: reject an invalid Host before reading any body.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.state.templates = Jinja2Templates(directory="templates")
    app.state.templates.env.globals["app_asset_version"] = os.stat(
        "static/app.js"
    ).st_mtime_ns
    app.state.config = config

    app.include_router(json_router)
    app.include_router(stream_router)
    app.include_router(views_router)

    @app.get("/api/meta/build")
    def build_identity():
        state_exists = STATE_DIR.exists()
        state_mounted = state_exists and os.path.ismount(STATE_DIR)
        return {
            "commit": os.environ.get("BUILD_COMMIT", "development"),
            "built_at": os.environ.get("BUILD_TIME", "unknown"),
            "deployment": deployment_mode(),
            "config_version": config_version(),
            "state": {
                "path": str(STATE_DIR),
                "mounted": state_mounted,
                "activation_marker": ACTIVATION_FILE.exists(),
                "legacy_state": (
                    not ACTIVATION_FILE.exists()
                    and AUTH_FILE.exists()
                    and OPERATOR_FILE.exists()
                ),
                "activated": setup_complete(),
            },
        }

    @app.get("/live")
    def live():
        """Process liveness: always 200 while the process can serve requests."""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request):
        """Dependency-aware readiness with bounded upstream checks.

        Returns 503 while the orchestrator is unreachable/not ready or the
        database connection fails; the health check only passes when every
        required dependency answers.
        """
        dependencies: dict[str, Any] = {}
        try:
            upstream = await request.app.state.orchestrator_client.get(
                f"{orchestrator_url()}/health",
                timeout=2.0,
            )
            dependencies["orchestrator"] = str(upstream.status_code)
            if upstream.status_code != 200:
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "unready",
                        "dependencies": dependencies,
                        "config_version": config_version(),
                    },
                )
            body = upstream.json()
            if not isinstance(body, dict) or body.get("readiness") != "ready":
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "unready",
                        "dependencies": dependencies,
                        "config_version": config_version(),
                    },
                )
            orchestrator_version = body.get("config_version")
            active_version = config_version()
            version_mismatch = bool(
                orchestrator_version
                and active_version
                and orchestrator_version != active_version
            )
            dependencies["config_version"] = {
                "api": active_version,
                "orchestrator": orchestrator_version,
                "mismatch": version_mismatch,
            }
            if version_mismatch:
                # Mixed config across processes can corrupt behavior; a
                # rolling-start transient is reported as unready until the
                # restart completes.
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "unready",
                        "dependencies": dependencies,
                        "config_version": active_version,
                    },
                )
        except Exception:
            dependencies["orchestrator"] = "unreachable"
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unready",
                    "dependencies": dependencies,
                    "config_version": config_version(),
                },
            )
        try:
            db_ok = await run_in_threadpool(check_connection)
        except Exception:
            db_ok = False
        dependencies["database"] = "ok" if db_ok else "unavailable"
        if not db_ok:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unready",
                    "dependencies": dependencies,
                    "config_version": config_version(),
                },
            )
        return {
            "status": "ok",
            "dependencies": dependencies,
            "config_version": config_version(),
        }

    return app


app = create_app()
