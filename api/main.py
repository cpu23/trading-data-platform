import base64
import hashlib
import hmac
import json
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import (
    ACTIVATION_FILE,
    AUTH_FILE,
    CSRF_COOKIE,
    OPERATOR_FILE,
    STATE_DIR,
    load_session_secret,
    migrate_legacy_state,
    mint_csrf_token,
    setup_complete,
    verify_credentials,
    verify_csrf_token,
)
from config import load_config
from logging_config import setup_logging
from routes.json import router as json_router
from routes.stream import stream_router
from routes.views import router as views_router


def create_app(
    orchestrator_client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> FastAPI:
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

    session_secret = load_session_secret()
    session_max_age = int(os.environ.get("SESSION_MAX_AGE_SECONDS", "43200"))
    auth_exempt_prefixes = ("/setup", "/login", "/api/setup", "/api/login", "/static")

    @app.middleware("http")
    async def signed_session(request: Request, call_next):
        session = {}
        cookie = request.cookies.get("market_session")
        if cookie:
            try:
                encoded, signature = cookie.rsplit(".", 1)
                expected = hmac.new(
                    session_secret, encoded.encode(), hashlib.sha256
                ).hexdigest()
                if hmac.compare_digest(signature, expected):
                    session = json.loads(base64.urlsafe_b64decode(encoded + "=="))
                    issued_at = int(session.get("issued_at", 0))
                    if session.get("authenticated") and (
                        not issued_at
                        or int(datetime.now(UTC).timestamp()) - issued_at
                        > session_max_age
                    ):
                        session = {}
            except Exception:
                session = {}
        request.scope["session"] = session
        before = dict(session)
        response = await call_next(request)
        if before and not session:
            response.delete_cookie("market_session", path="/")
        elif session != before:
            encoded = (
                base64.urlsafe_b64encode(json.dumps(session).encode())
                .decode()
                .rstrip("=")
            )
            signature = hmac.new(
                session_secret, encoded.encode(), hashlib.sha256
            ).hexdigest()
            response.set_cookie(
                "market_session",
                f"{encoded}.{signature}",
                httponly=True,
                samesite="strict",
                secure=os.environ.get("COOKIE_SECURE") == "1",
                max_age=session_max_age,
                path="/",
            )
        return response

    @app.middleware("http")
    async def csrf_contract(request: Request, call_next):
        path = request.url.path
        is_exempt = any(path.startswith(prefix) for prefix in auth_exempt_prefixes)
        # Skip CSRF for requests without auth credentials; let auth dependency return 401
        has_auth = bool(
            request.headers.get("authorization")
            or request.scope.get("session", {}).get("authenticated")
        )
        if not has_auth and not is_exempt:
            return await call_next(request)
        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        token = cookie_token if verify_csrf_token(cookie_token) else mint_csrf_token()
        request.state.csrf_token = token
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not is_exempt:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            browser_signal = bool(
                origin
                or referer
                or request.cookies.get(CSRF_COOKIE)
                or request.headers.get("sec-fetch-site")
            )
            machine_json = (
                request.headers.get("content-type", "").split(";", 1)[0].lower()
                == "application/json"
                and not browser_signal
            )
            if not machine_json:
                supplied = request.headers.get("x-csrf-token", "")
                from urllib.parse import urlsplit

                expected_origin = (
                    urlsplit(str(request.base_url)).scheme,
                    urlsplit(str(request.base_url)).netloc,
                )
                supplied_origin = origin or referer
                parsed = urlsplit(supplied_origin) if supplied_origin else None
                same_origin = (
                    parsed and (parsed.scheme, parsed.netloc) == expected_origin
                )
                if not verify_csrf_token(supplied) or not same_origin:
                    return JSONResponse(
                        status_code=403, content={"detail": "CSRF validation failed"}
                    )
        response = await call_next(request)
        if (
            request.method == "GET"
            and response.status_code < 400
            and path not in {"/static", "/api/quotes/stream", "/stream"}
        ):
            secure = os.environ.get("COOKIE_SECURE", "0").lower() in {
                "1",
                "true",
                "yes",
            }
            response.set_cookie(
                CSRF_COOKIE,
                token or mint_csrf_token(),
                secure=secure,
                httponly=False,
                samesite="strict",
                path="/",
            )
        return response

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
            "deployment": os.environ.get("DEPLOYMENT_MODE", "local"),
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

    @app.get("/ready")
    def ready():
        return {"status": "ok"}

    return app


app = create_app()
