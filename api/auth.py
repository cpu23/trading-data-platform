import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path

from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic(auto_error=False)
CSRF_COOKIE = "csrf-token"
SSE_PURPOSE = "quotes-stream"
CSRF_PURPOSE = "csrf"

STATE_DIR = Path(os.environ.get("STATE_DIR", "/app/state"))
AUTH_FILE = STATE_DIR / "auth.json"
ACTIVATION_FILE = STATE_DIR / "activated.json"
OPERATOR_FILE = STATE_DIR / "operator.yaml"
SESSION_SECRET_FILE = STATE_DIR / "session_secret"
STATE_FILENAMES = (
    "auth.json",
    "operator.yaml",
    "secrets.env",
    "activated.json",
    "session_secret",
)



def is_html_request(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return (
        request.method in {"GET", "HEAD"}
        and not request.url.path.startswith("/api/")
        and "text/html" in accept
    )


def login_redirect(request: Request) -> HTTPException:
    destination = request.url.path
    if request.url.query:
        destination += f"?{request.url.query}"
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"/login?next={quote(destination, safe='/')}"},
    )


def verify_credentials(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> str:
    public_paths = {
        "/setup",
        "/api/setup/status",
        "/api/setup/activate",
        "/login",
        "/api/login",
        "/api/meta/build",
        "/api/quotes/stream",
        "/ready",
    }
    if request.url.path in public_paths or request.url.path.startswith("/static/"):
        return "bootstrap"
    if request.url.path == "/" and not setup_complete():
        return "bootstrap"
    session = request.scope.get("session", {}) if hasattr(request, "scope") else getattr(request, "session", {})
    if session.get("authenticated"):
        return "admin"
    if setup_complete():
        if is_html_request(request):
            raise login_redirect(request)
        raise HTTPException(status_code=401, detail="Login required")

    expected_user = os.environ.get("DASHBOARD_USER", "")
    expected_pass = os.environ.get("DASHBOARD_PASSWORD", "")
    legacy_enabled = os.environ.get("LEGACY_BASIC_AUTH", "").lower() in {"1", "true", "yes"}
    if legacy_enabled and credentials and expected_user and expected_pass:
        if secrets.compare_digest(credentials.username, expected_user) and secrets.compare_digest(credentials.password, expected_pass):
            return credentials.username
    if is_html_request(request):
        raise login_redirect(request)
    headers = {"WWW-Authenticate": "Basic"} if legacy_enabled else None
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Setup required" if not legacy_enabled else "Invalid credentials",
        headers=headers,
    )


def _secret() -> bytes:
    raw = os.environ.get("SSE_SIGNING_KEY") or os.environ.get("DASHBOARD_PASSWORD")
    return raw.encode("utf-8") if raw else b""


def _signed(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    secret = _secret()
    if not secret:
        raise RuntimeError("signing key unavailable")
    return f"{encoded}.{hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()}"


def mint_sse_token(path: str = "/api/quotes/stream", ttl: int = 60) -> str:
    payload = {"path": path, "purpose": SSE_PURPOSE, "exp": int(time.time()) + ttl, "jti": uuid.uuid4().hex}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return _signed(payload)


def mint_csrf_token(ttl: int = 3600) -> str:
    return _signed({"purpose": CSRF_PURPOSE, "exp": int(time.time()) + ttl, "jti": uuid.uuid4().hex})


def verify_sse_token(token: str | None, path: str) -> bool:
    if not token or not _secret():
        return False
    try:
        encoded, supplied = token.split(".", 1)
        expected = hmac.new(_secret(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            return False
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        valid = payload.get("path") == path and payload.get("purpose") == SSE_PURPOSE and int(payload.get("exp", 0)) > int(time.time())
        return valid
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, OverflowError):
        return False


def verify_csrf_token(token: str | None) -> bool:
    if not token or not _secret():
        return False
    try:
        encoded, supplied = token.split(".", 1)
        expected = hmac.new(_secret(), encoded.encode(), hashlib.sha256).hexdigest()
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return hmac.compare_digest(supplied, expected) and payload.get("purpose") == CSRF_PURPOSE and int(payload.get("exp", 0)) > int(time.time())
    except (ValueError, TypeError, json.JSONDecodeError, OverflowError):
        return False



def hash_password(password: str) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(digest).decode()}


def verify_password(password: str, record: dict) -> bool:
    salt = base64.b64decode(record["salt"])
    actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return secrets.compare_digest(base64.b64encode(actual).decode(), record["hash"])


def create_admin(password: str) -> None:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = AUTH_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(hash_password(password)))
    temporary.chmod(0o600)
    temporary.replace(AUTH_FILE)


def setup_complete() -> bool:
    if ACTIVATION_FILE.exists():
        return AUTH_FILE.exists() and OPERATOR_FILE.exists()
    return AUTH_FILE.exists() and OPERATOR_FILE.exists()


def load_session_secret() -> bytes:
    configured = os.environ.get("SESSION_SECRET")
    if configured:
        return configured.encode()
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not SESSION_SECRET_FILE.exists():
        temporary = SESSION_SECRET_FILE.with_suffix(".tmp")
        temporary.write_text(secrets.token_urlsafe(48))
        temporary.chmod(0o600)
        temporary.replace(SESSION_SECRET_FILE)
    return SESSION_SECRET_FILE.read_text().strip().encode()


def migrate_legacy_state() -> bool:
    legacy_value = os.environ.get("LEGACY_STATE_DIR", "")
    if not legacy_value:
        return False
    legacy_dir = Path(legacy_value)
    if not legacy_dir.is_dir():
        return False
    if AUTH_FILE.exists() and OPERATOR_FILE.exists():
        if ACTIVATION_FILE.exists():
            return False
        temporary = ACTIVATION_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({"migrated": True, "version": 1}))
        temporary.chmod(0o600)
        temporary.replace(ACTIVATION_FILE)
        return True
    if any((STATE_DIR / name).exists() for name in STATE_FILENAMES):
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied = False
    for name in STATE_FILENAMES:
        source = legacy_dir / name
        if source.exists():
            destination = STATE_DIR / name
            shutil.copy2(source, destination)
            destination.chmod(0o600)
            copied = True
    if copied:
        temporary = ACTIVATION_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({"migrated": True, "version": 1}))
        temporary.chmod(0o600)
        temporary.replace(ACTIVATION_FILE)
    return copied