from fastapi import APIRouter, Request
from auth import setup_complete
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
    return request.app.state.templates.TemplateResponse(
        request, "settings.html", {"request": request, "config": load_config()}
    )
