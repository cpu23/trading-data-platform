from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from config import load_config

router = APIRouter()

TIMEZONE_CHOICES = (
    "UTC",
    "Europe/London",
    "America/New_York",
    "Asia/Tokyo",
    "Australia/Sydney",
)
TimezoneName = Literal[
    "UTC",
    "Europe/London",
    "America/New_York",
    "Asia/Tokyo",
    "Australia/Sydney",
]
COOKIE_NAME = "display_timezone"


class TimezoneUpdate(BaseModel):
    timezone: TimezoneName


def configured_timezone(config: dict) -> str:
    candidate = config.get("timezone", {}).get("primary", {}).get("name", "UTC")
    return candidate if candidate in TIMEZONE_CHOICES else "UTC"


def current_timezone_name(request: Request, config: dict | None = None) -> str:
    candidate = request.cookies.get(COOKIE_NAME)
    if candidate in TIMEZONE_CHOICES:
        return candidate
    return configured_timezone(config or load_config())


def timezone_context(request: Request, config: dict | None = None) -> dict:
    name = current_timezone_name(request, config)
    # ZoneInfo is reached only after strict allowlist resolution.
    return {
        "current_timezone": name,
        "timezone_choices": list(TIMEZONE_CHOICES),
        "display_zone": ZoneInfo(name),
    }


@router.get("/settings/timezone")
def get_timezone_setting(request: Request):
    context = timezone_context(request)
    return {"current": context["current_timezone"], "choices": context["timezone_choices"]}


@router.post("/settings/timezone")
def set_timezone_setting(update: TimezoneUpdate, response: Response):
    response.set_cookie(
        COOKIE_NAME,
        update.timezone,
        httponly=True,
        secure=False,
        samesite="strict",
        path="/",
    )
    return {"current": update.timezone, "choices": list(TIMEZONE_CHOICES)}
