import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()
CSRF_COOKIE = "csrf-token"
SSE_PURPOSE = "quotes-stream"
CSRF_PURPOSE = "csrf"


def _get_expected_credentials() -> tuple[str, str]:
    username = os.environ.get("DASHBOARD_USER", "")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not username or not password:
        raise RuntimeError(
            "DASHBOARD_USER and DASHBOARD_PASSWORD environment variables must be set"
        )
    return username, password


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    expected_user, expected_pass = _get_expected_credentials()

    username_match = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        expected_user.encode("utf-8"),
    )
    password_match = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        expected_pass.encode("utf-8"),
    )

    if not (username_match and password_match):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


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
