import os
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from auth import setup_complete, STATE_DIR
from config import load_config

router = APIRouter()

@router.get("/setup")
def setup_page(request: Request):
    if setup_complete():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")
    return request.app.state.templates.TemplateResponse(request, "setup.html", {"request": request})


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if not setup_complete():
        return RedirectResponse("/setup", status_code=303)
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=303)
    parsed = urlparse(next)
    safe_next = next if not parsed.scheme and not parsed.netloc and next.startswith("/") else "/"
    return request.app.state.templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "next_path": safe_next},
    )

@router.get("/settings")
def settings_page(request: Request):
    config = load_config()
    secrets_path = STATE_DIR / "secrets.env"
    saved_secrets = set()
    if secrets_path.exists():
        saved_secrets = {
            line.partition("=")[0]
            for line in secrets_path.read_text().splitlines()
            if "=" in line and line.partition("=")[2]
        }
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "config": config,
            "has_llm_key": "LLM_API_KEY" in saved_secrets,
            "has_fred_key": "FRED_API_KEY" in saved_secrets,
            "has_oanda_key": "OANDA_API_KEY" in saved_secrets,
            "has_eia_key": "EIA_API_KEY" in saved_secrets,
            "fred_key_source": (
                "setup" if "FRED_API_KEY" in saved_secrets
                else "environment" if os.environ.get("FRED_API_KEY")
                else None
            ),
            "oanda_key_source": (
                "setup" if "OANDA_API_KEY" in saved_secrets
                else "environment" if os.environ.get("OANDA_API_KEY")
                else None
            ),
            "eia_key_source": (
                "setup" if "EIA_API_KEY" in saved_secrets
                else "environment" if os.environ.get("EIA_API_KEY")
                else None
            ),
        },
    )
