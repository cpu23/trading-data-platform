import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic(auto_error=False)
STATE_DIR = Path(os.environ.get("STATE_DIR", "/app/state"))
AUTH_FILE = STATE_DIR / "auth.json"


def hash_password(password: str) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(digest).decode()}


def verify_password(password: str, record: dict) -> bool:
    salt = base64.b64decode(record["salt"])
    actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return secrets.compare_digest(base64.b64encode(actual).decode(), record["hash"])


def setup_complete() -> bool:
    return AUTH_FILE.exists()


def create_admin(password: str) -> None:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = AUTH_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(hash_password(password)))
    temporary.chmod(0o600)
    temporary.replace(AUTH_FILE)


def verify_credentials(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> str:
    if request.url.path.startswith(("/setup", "/api/setup", "/login", "/static/")):
        return "bootstrap"
    if request.url.path == "/" and not setup_complete():
        return "bootstrap"
    if request.session.get("authenticated"):
        return "admin"
    if setup_complete():
        raise HTTPException(status_code=401, detail="Login required")

    # Migration compatibility for existing installations before setup is completed.
    expected_user = os.environ.get("DASHBOARD_USER", "")
    expected_pass = os.environ.get("DASHBOARD_PASSWORD", "")
    if credentials and expected_user and expected_pass:
        if secrets.compare_digest(credentials.username, expected_user) and secrets.compare_digest(credentials.password, expected_pass):
            return credentials.username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )
