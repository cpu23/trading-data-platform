from fastapi import APIRouter, Request
from auth import setup_complete, STATE_DIR
from config import load_config

router = APIRouter()

@router.get("/setup")
def setup_page(request: Request):
    if setup_complete():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")
    return request.app.state.templates.TemplateResponse(request, "setup.html", {"request": request})

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
        },
    )
