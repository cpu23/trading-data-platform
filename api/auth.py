import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()


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
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username