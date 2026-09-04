from api_logging import get_logger
from data_quality import (
    evaluate_quality,
    normalize_quality_results,
    required_quality_checks,
    run_quality_checks,
)
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from config import load_config

router = APIRouter()

logger = get_logger("quality.view")


def _get_templates(request: Request):
    return request.app.state.templates


def _load_quality_data():
    config = load_config()
    results = run_quality_checks(config)
    required = required_quality_checks(config)
    overall = evaluate_quality(results, required)
    normalized = normalize_quality_results(results)
    checks = []
    if isinstance(normalized, dict):
        for check_id, check_data in normalized.items():
            checks.append(
                {
                    "name": check_id.replace("_", " "),
                    "healthy": check_data.get("healthy", False),
                    "detail": check_data.get("detail", ""),
                }
            )
    elif isinstance(normalized, list):
        checks = normalized
    return overall, checks


@router.get("/quality")
async def quality_page(request: Request):
    templates = _get_templates(request)
    overall = None
    checks = []
    error = None

    try:
        overall, checks = await run_in_threadpool(_load_quality_data)
    except Exception as exc:
        error = str(exc)
        logger.error("quality_fetch_error", error=str(exc))

    logger.info(
        "quality_page_rendered",
        overall=overall,
        check_count=len(checks),
        error=bool(error),
    )

    return templates.TemplateResponse(
        request,
        "quality.html",
        {
            "request": request,
            "overall": overall,
            "checks": checks,
            "error": error,
        },
    )
