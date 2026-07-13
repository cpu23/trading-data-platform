from logging_config import get_logger

import httpx
from fastapi import APIRouter, Request

router = APIRouter()

logger = get_logger("quality.view")


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
                # The orchestrator returns checks as a dict, convert to list
                raw_checks = data.get("checks", {})
                if isinstance(raw_checks, dict):
                    for check_id, check_data in raw_checks.items():
                        checks.append({
                            "name": check_id.replace("_", " "),
                            "healthy": check_data.get("healthy", False),
                            "detail": check_data.get("detail", ""),
                        })
                elif isinstance(raw_checks, list):
                    checks = raw_checks
            else:
                error = f"Orchestrator returned {resp.status_code}"
                logger.error("quality_fetch_failed", status=resp.status_code)
    except Exception as exc:
        error = str(exc)
        logger.error("quality_fetch_error", error=str(exc))

    logger.info("quality_page_rendered", overall=overall, check_count=len(checks), error=bool(error))

    return templates.TemplateResponse(request, "quality.html", {
        "request": request,
        "overall": overall,
        "checks": checks,
        "error": error,
    })
