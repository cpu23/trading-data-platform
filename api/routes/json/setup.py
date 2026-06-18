import os
from pathlib import Path
import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from auth import create_admin, setup_complete, verify_password, AUTH_FILE, STATE_DIR
from config import reload_config

router = APIRouter()

COVERAGE_SOURCES = (
    "fred",
    "forex_factory",
    "cftc",
    "oecd",
    "central_banks",
    "ecb",
    "boe",
    "eia",
)
DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LLM_MODEL = "deepseek/deepseek-v4-flash"


def _coverage_config(selection: dict | None) -> dict:
    selection = selection or {}
    return {
        source_id: {"enabled": bool(selection.get(source_id, False))}
        for source_id in COVERAGE_SOURCES
    }


def _write_private_yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(yaml.safe_dump(value, sort_keys=False))
    temporary.chmod(0o600)
    temporary.replace(path)


def _merge_profile(base: dict, update: dict) -> dict:
    result = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_profile(result[key], value)
        else:
            result[key] = value
    return result


def _read_secrets() -> dict[str, str]:
    path = STATE_DIR / "secrets.env"
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _write_secrets(updates: dict) -> None:
    values = _read_secrets()
    for key, value in updates.items():
        if value:
            values[str(key)] = str(value)
    path = STATE_DIR / "secrets.env"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    temporary.chmod(0o600)
    temporary.replace(path)


@router.get("/setup/status")
def status():
    return {"setup_complete": setup_complete(), "demo_available": True}

@router.post("/setup/activate")
def activate(body: dict, request: Request):
    if setup_complete(): raise HTTPException(409, "Setup is locked")
    try:
        create_admin(str(body.get("password", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    profile = body.get("profile") or {}
    profile["collectors"] = _coverage_config(body.get("coverage"))
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = STATE_DIR / "operator.yaml"
    _write_private_yaml(path, profile)
    _write_secrets(body.get("secrets") or {})
    reload_config()
    request.session["authenticated"] = True
    request.session["csrf"] = os.urandom(24).hex()
    return {"activated": True, "csrf_token": request.session["csrf"]}

@router.put("/setup/profile")
def update_profile(body: dict, request: Request):
    if not request.session.get("authenticated"): raise HTTPException(401, "Login required")
    path = STATE_DIR / "operator.yaml"
    profile = body.get("profile") or {}
    if "coverage" in body:
        profile["collectors"] = _coverage_config(body.get("coverage"))
    existing = yaml.safe_load(path.read_text()) or {} if path.exists() else {}
    profile = _merge_profile(existing, profile)
    _write_private_yaml(path, profile)
    _write_secrets(body.get("secrets") or {})
    reload_config()
    return {"saved": True, "restart_required": False}


@router.post("/setup/test-connection")
def test_connection(body: dict, request: Request):
    if not request.session.get("authenticated") and setup_complete():
        raise HTTPException(401, "Login required")
    base_url = str(body.get("base_url") or DEFAULT_LLM_BASE_URL).rstrip("/")
    api_key = str(body.get("api_key") or _read_secrets().get("LLM_API_KEY") or "")
    if not api_key:
        raise HTTPException(400, "Add an API key before testing the connection")
    try:
        response = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(400, f"Provider rejected the connection ({exc.response.status_code})") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(400, "Could not reach the provider endpoint") from exc
    return {"connected": True}

@router.post("/login")
def login(body: dict, request: Request):
    if not setup_complete(): raise HTTPException(409, "Setup not complete")
    record = __import__("json").loads(AUTH_FILE.read_text())
    if not verify_password(str(body.get("password", "")), record):
        raise HTTPException(401, "Invalid credentials")
    request.session["authenticated"] = True
    request.session["csrf"] = os.urandom(24).hex()
    return {"authenticated": True, "csrf_token": request.session["csrf"]}

@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"authenticated": False}
