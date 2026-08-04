import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from config import load_config, reload_config

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
STATE_DIR = Path(os.environ.get("OPERATOR_STATE_DIR", "/app/state"))
OPERATOR_CONFIG = STATE_DIR / "operator.yaml"
SECRETS_FILE = STATE_DIR / "secrets.env"
ALLOWED_SECRET_KEYS = {
    "OPENROUTER_API_KEY",
    "FRED_API_KEY",
    "OANDA_API_KEY",
    "TWITTERAPIKEY",
    "TWITTERAPI_KEY",
}
ALLOWED_PROCESSORS = {"macro_regime", "event_impact", "briefing"}


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
    return {
        "current_timezone": name,
        "timezone_choices": list(TIMEZONE_CHOICES),
        "display_zone": ZoneInfo(name),
    }


def _atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content)
    temporary.chmod(0o600)
    temporary.replace(path)


def _read_secrets() -> dict[str, str]:
    if not SECRETS_FILE.exists():
        return {}
    values = {}
    for line in SECRETS_FILE.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _write_secrets(updates: dict) -> None:
    values = _read_secrets()
    for key, value in updates.items():
        normalized = "TWITTERAPI_KEY" if key == "TWITTERAPIKEY" else str(key)
        if (
            normalized in ALLOWED_SECRET_KEYS
            and isinstance(value, str)
            and value.strip()
        ):
            values[normalized] = value.strip()
    _atomic_private_write(
        SECRETS_FILE,
        "".join(f"{key}={value}\n" for key, value in sorted(values.items())),
    )


@router.get("/settings/timezone")
def get_timezone_setting(request: Request):
    context = timezone_context(request)
    return {
        "current": context["current_timezone"],
        "choices": context["timezone_choices"],
    }


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


@router.put("/settings/operator")
def update_operator_settings(body: dict):
    llm = body.get("llm") if isinstance(body.get("llm"), dict) else {}
    default_model = str(llm.get("default_model") or "").strip()
    if not default_model or len(default_model) > 200:
        raise HTTPException(422, "A valid default model is required")
    models = llm.get("models") if isinstance(llm.get("models"), dict) else {}
    clean_models = {
        key: str(value).strip()
        for key, value in models.items()
        if key in ALLOWED_PROCESSORS
        and isinstance(value, str)
        and value.strip()
        and len(value) <= 200
    }
    try:
        daily_budget = float(body.get("daily_budget_usd"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Daily budget must be numeric")
    if not 0 <= daily_budget <= 1000:
        raise HTTPException(422, "Daily budget must be between 0 and 1000")
    operator = {
        "llm": {"default_model": default_model, "models": clean_models},
        "budgets": {"daily_llm_usd": daily_budget},
    }
    _atomic_private_write(OPERATOR_CONFIG, yaml.safe_dump(operator, sort_keys=False))
    _write_secrets(body.get("secrets") if isinstance(body.get("secrets"), dict) else {})
    reload_config()
    return {"saved": True, "applies_to_next_run": True}


@router.post("/settings/test-openrouter")
def test_openrouter(body: dict):
    supplied = str(body.get("api_key") or "").strip()
    api_key = (
        supplied
        or _read_secrets().get("OPENROUTER_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY", "")
    )
    if not api_key:
        raise HTTPException(400, "Add an OpenRouter key before testing")
    try:
        response = httpx.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            400, f"OpenRouter rejected the key ({exc.response.status_code})"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(400, "Could not reach OpenRouter") from exc
    return {"connected": True}
