import os
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlparse
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import (
    ACTIVATION_FILE,
    AUTH_FILE,
    OPERATOR_FILE,
    STATE_DIR,
    migrate_legacy_state,
    load_session_secret,
    setup_complete,
    verify_credentials,
)
from config import load_config
from logging_config import setup_logging
from routes.json import router as json_router
from routes.views import router as views_router


def create_app() -> FastAPI:
    migrate_legacy_state()
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

    session_secret = load_session_secret()
    session_max_age = int(os.environ.get("SESSION_MAX_AGE_SECONDS", "43200"))

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
                    issued_at = int(session.get("issued_at", 0))
                    if session.get("authenticated") and (
                        not issued_at
                        or int(datetime.now(timezone.utc).timestamp()) - issued_at > session_max_age
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
            encoded = base64.urlsafe_b64encode(json.dumps(session).encode()).decode().rstrip("=")
            signature = hmac.new(session_secret, encoded.encode(), hashlib.sha256).hexdigest()
            response.set_cookie(
                "market_session", f"{encoded}.{signature}", httponly=True,
                samesite="strict", secure=os.environ.get("COOKIE_SECURE") == "1",
                max_age=session_max_age, path="/",
            )
        return response

    @app.middleware("http")
    async def protect_state_changes(request: Request, call_next):
        csrf_exempt = {
            "/api/login",
            "/api/setup/activate",
            "/api/setup/test-connection",
        }
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path not in csrf_exempt:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).hostname != request.url.hostname:
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
    def readiness():
        state_ready = STATE_DIR.exists() and STATE_DIR.is_dir()
        persistent_required = os.environ.get("REQUIRE_PERSISTENT_STATE", "").lower() in {"1", "true", "yes"}
        state_persistent = os.path.ismount(STATE_DIR)
        activation_required = os.environ.get("REQUIRE_ACTIVATED_STATE", "").lower() in {"1", "true", "yes"}
        activated = setup_complete()
        ready = (
            state_ready
            and (state_persistent or not persistent_required)
            and (activated or not activation_required)
        )
        payload = {
            "status": "ready" if ready else "not_ready",
            "state_mounted": state_ready,
            "state_persistent": state_persistent,
            "activated": activated,
        }
        return JSONResponse(payload, status_code=200 if ready else 503)

    return app


app = create_app()
