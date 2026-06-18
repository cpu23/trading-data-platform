import os
from pathlib import Path
import yaml
from fastapi import APIRouter, HTTPException, Request
from auth import create_admin, setup_complete, verify_password, AUTH_FILE, STATE_DIR
from config import reload_config

router = APIRouter()

@router.get("/setup/status")
def status():
    return {"setup_complete": setup_complete(), "demo_available": True}

@router.post("/setup/activate")
def activate(body: dict, request: Request):
    if setup_complete(): raise HTTPException(409, "Setup is locked")
    create_admin(str(body.get("password", "")))
    profile = body.get("profile") or {}
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = STATE_DIR / "operator.yaml"
    path.write_text(yaml.safe_dump(profile, sort_keys=False))
    path.chmod(0o600)
    secrets_path = STATE_DIR / "secrets.env"
    secret_values = body.get("secrets") or {}
    secrets_path.write_text("\n".join(f"{key}={value}" for key, value in secret_values.items()) + "\n")
    secrets_path.chmod(0o600)
    reload_config()
    request.session["authenticated"] = True
    request.session["csrf"] = os.urandom(24).hex()
    return {"activated": True, "csrf_token": request.session["csrf"]}

@router.put("/setup/profile")
def update_profile(body: dict, request: Request):
    if not request.session.get("authenticated"): raise HTTPException(401, "Login required")
    path = STATE_DIR / "operator.yaml"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(yaml.safe_dump(body, sort_keys=False))
    temporary.chmod(0o600)
    temporary.replace(path)
    return {"saved": True, "restart_required": False}

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
