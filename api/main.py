import os
import base64
import hashlib
import hmac
import json
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import verify_credentials
from config import load_config
from logging_config import setup_logging
from routes.json import router as json_router
from routes.views import router as views_router


def create_app() -> FastAPI:
    config = load_config()
    log_level = config.get("logging", {}).get("level", "INFO")
    setup_logging(level=log_level)

    app = FastAPI(
        title="Trading Data API",
        version="0.1.0",
        dependencies=[Depends(verify_credentials)],
    )

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.environ.get("TRUSTED_HOSTS", "localhost,127.0.0.1,testserver").split(","))
    app.add_middleware(CORSMiddleware, allow_origins=[], allow_credentials=False, allow_methods=[], allow_headers=[])

    session_secret = os.environ.get("SESSION_SECRET", "development-change-me").encode()

    @app.middleware("http")
    async def signed_session(request: Request, call_next):
        session = {}
        cookie = request.cookies.get("market_session")
        if cookie:
            try:
                encoded, signature = cookie.rsplit(".", 1)
                expected = hmac.new(session_secret, encoded.encode(), hashlib.sha256).hexdigest()
                if hmac.compare_digest(signature, expected):
                    session = json.loads(base64.urlsafe_b64decode(encoded + "=="))
            except Exception:
                session = {}
        request.scope["session"] = session
        before = dict(session)
        response = await call_next(request)
        if session != before:
            encoded = base64.urlsafe_b64encode(json.dumps(session).encode()).decode().rstrip("=")
            signature = hmac.new(session_secret, encoded.encode(), hashlib.sha256).hexdigest()
            response.set_cookie(
                "market_session", f"{encoded}.{signature}", httponly=True,
                samesite="strict", secure=os.environ.get("COOKIE_SECURE") == "1",
            )
        return response

    @app.middleware("http")
    async def protect_state_changes(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not request.url.path.startswith(("/login", "/api/login", "/api/setup")):
            origin = request.headers.get("origin")
            if origin and request.url.hostname not in origin:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Origin rejected"}, status_code=403)
            session = request.scope.get("session", {})
            if not session and request.cookies.get("market_session"):
                try:
                    encoded, signature = request.cookies["market_session"].rsplit(".", 1)
                    expected = hmac.new(session_secret, encoded.encode(), hashlib.sha256).hexdigest()
                    if hmac.compare_digest(signature, expected):
                        session = json.loads(base64.urlsafe_b64decode(encoded + "=="))
                except Exception:
                    session = {}
            if session.get("authenticated") and request.headers.get("x-csrf-token") != session.get("csrf"):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "CSRF token required"}, status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.state.templates = Jinja2Templates(directory="templates")

    app.include_router(json_router)
    app.include_router(views_router)

    return app


app = create_app()
