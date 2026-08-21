from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from auth import demo_basic_auth_configured, setup_complete

router = APIRouter()


@router.get("/setup")
def setup_page(request: Request):
    if demo_basic_auth_configured():
        # Demo deployments authenticate with the configured HTTP Basic
        # credentials (demo/demo) and must never present the setup form.
        return RedirectResponse("/", status_code=303)
    if setup_complete():
        return RedirectResponse("/")
    return request.app.state.templates.TemplateResponse(
        request, "setup.html", {"request": request}
    )


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if not setup_complete():
        if demo_basic_auth_configured():
            # A fresh demo volume has no setup state; bounce to the root so
            # the browser receives the native Basic challenge (demo/demo).
            return RedirectResponse("/", status_code=303)
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
