import httpx
from fastapi import APIRouter, Request

router = APIRouter()


def _get_templates(request: Request):
    return request.app.state.templates


@router.get("/quality")
def quality_page(request: Request):
    templates = _get_templates(request)
    overall = None
    checks = []
    error = None

    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get("http://orchestrator:8000/quality")
            if resp.is_success:
                data = resp.json()
                overall = data.get("overall")
                checks = data.get("checks", [])
            else:
                error = f"Orchestrator returned {resp.status_code}"
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(request, "quality.html", {
        "request": request,
        "overall": overall,
        "checks": checks,
        "error": error,
    })
