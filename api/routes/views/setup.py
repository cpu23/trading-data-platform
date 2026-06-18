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
    has_llm_key = False
    if secrets_path.exists():
        has_llm_key = any(
            line.startswith("LLM_API_KEY=") and line.partition("=")[2]
            for line in secrets_path.read_text().splitlines()
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {"request": request, "config": config, "has_llm_key": has_llm_key},
    )
