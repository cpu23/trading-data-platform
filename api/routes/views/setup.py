from urllib.parse import urlparse

from auth import setup_complete
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/setup")
def setup_page(request: Request):
    if setup_complete():
        return RedirectResponse("/")
    return request.app.state.templates.TemplateResponse(
        request, "setup.html", {"request": request}
    )


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if not setup_complete():
        return RedirectResponse("/setup", status_code=303)
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=303)
    parsed = urlparse(next)
    safe_next = (
        next
        if not parsed.scheme and not parsed.netloc and next.startswith("/")
        else "/"
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "next_path": safe_next},
    )
