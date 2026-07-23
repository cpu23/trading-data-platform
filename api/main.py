from collections.abc import Callable
from contextlib import asynccontextmanager

import httpx
import os
import base64
import binascii
import secrets
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import CSRF_COOKIE, mint_csrf_token, verify_csrf_token
from config import load_config
from logging_config import setup_logging
from routes.json import router as json_router
from routes.views import router as views_router


def create_app(
    orchestrator_client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> FastAPI:
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
    )

    @app.middleware("http")
    async def security_contract(request: Request, call_next):
        is_sse = request.url.path == "/api/quotes/stream"
        if not is_sse:
            auth = request.headers.get("authorization", "")
            try:
                scheme, value = auth.split(" ", 1)
                user, password = base64.b64decode(value, validate=True).decode().split(":", 1)
                expected_user, expected_password = os.environ.get("DASHBOARD_USER", ""), os.environ.get("DASHBOARD_PASSWORD", "")
                valid = scheme.lower() == "basic" and expected_user and expected_password and secrets.compare_digest(user, expected_user) and secrets.compare_digest(password, expected_password)
            except (ValueError, UnicodeDecodeError, binascii.Error):
                valid = False
            if not valid:
                return JSONResponse(status_code=401, content={"detail": "Authentication required"}, headers={"WWW-Authenticate": "Basic"})
        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        token = cookie_token if verify_csrf_token(cookie_token) else mint_csrf_token()
        request.state.csrf_token = token
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            browser_signal = bool(origin or referer or request.cookies.get(CSRF_COOKIE) or request.headers.get("sec-fetch-site"))
            machine_json = request.headers.get("content-type", "").split(";", 1)[0].lower() == "application/json" and not browser_signal
            if not machine_json:
                supplied = request.headers.get("x-csrf-token", "")
                from urllib.parse import urlsplit
                expected_origin = (urlsplit(str(request.base_url)).scheme, urlsplit(str(request.base_url)).netloc)
                supplied_origin = origin or referer
                parsed = urlsplit(supplied_origin) if supplied_origin else None
                same_origin = parsed and (parsed.scheme, parsed.netloc) == expected_origin
                if not verify_csrf_token(supplied) or not same_origin:
                    return JSONResponse(status_code=403, content={"detail": "CSRF validation failed"})
        response = await call_next(request)
        if request.method == "GET" and response.status_code < 400 and request.url.path not in {"/static", "/api/quotes/stream"}:
            secure = os.environ.get("COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}
            response.set_cookie(CSRF_COOKIE, token or mint_csrf_token(), secure=secure, httponly=False, samesite="strict", path="/")
        return response

    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.state.templates = Jinja2Templates(directory="templates")

    app.include_router(json_router)
    app.include_router(views_router)

    return app


app = create_app()
