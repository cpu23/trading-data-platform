import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
import yaml
from fastapi import APIRouter, Body, HTTPException, Request, Response
from pydantic import BaseModel

from auth import setup_complete
from config import (
    config_status,
    load_config,
    orchestrator_url,
    reload_config,
)
from setup_state import (
    commit_setup,
    merge_profile,
    migrate_legacy_profile,
    parse_secrets_file,
    read_live_state,
    setup_lock,
)

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
STATE_DIR = Path(
    os.environ.get("STATE_DIR") or os.environ.get("OPERATOR_STATE_DIR") or "/app/state"
)
OPERATOR_CONFIG = STATE_DIR / "operator.yaml"
SECRETS_FILE = STATE_DIR / "secrets.env"
# Canonical secret names accepted in API payloads and written to secrets.env.
ALLOWED_SECRET_KEYS = {
    "OPENROUTER_API_KEY",
    "FRED_API_KEY",
    "OANDA_API_KEY",
    "EIA_API_KEY",
    "TWITTERAPI_KEY",
}
# Accepted write aliases, normalized to the canonical name.
SECRET_ALIASES = {
    "LLM_API_KEY": "OPENROUTER_API_KEY",
    "TWITTERAPIKEY": "TWITTERAPI_KEY",
}
MAX_SECRET_VALUE_LENGTH = 4096


def normalize_secret_name(raw: str) -> str:
    """Canonical secret name for a payload key; raises ``KeyError`` if unknown."""
    key = str(raw)
    if key in ALLOWED_SECRET_KEYS:
        return key
    if key in SECRET_ALIASES:
        return SECRET_ALIASES[key]
    raise KeyError(key)


def validate_secret_value(value: str) -> str:
    """Validate and normalize one secret value; raises ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError("secret values must be strings")
    stripped = value.strip()
    if not stripped:
        raise ValueError("secret values must not be empty")
    if len(stripped) > MAX_SECRET_VALUE_LENGTH:
        raise ValueError("secret value is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in stripped):
        raise ValueError("secret values must not contain control characters")
    return stripped


def apply_secret_updates(current: dict[str, str], updates: dict) -> dict[str, str]:
    """Apply explicit set / unchanged / delete semantics to a secret mapping.

    - non-empty string: set or replace (value is validated and stripped)
    - ``None``: logical deletion, stored as a canonical ``KEY=`` tombstone so
      loaders see the key as present-but-empty and fail closed instead of
      falling back to the process environment
    - omitted or empty string: unchanged (backward compatible with the UI
      sending every credential field, blank meaning "keep the saved value")
    Unknown keys raise ``KeyError``.
    """
    result = dict(current)
    for raw_key, value in updates.items():
        key = normalize_secret_name(raw_key)
        if value is None:
            result[key] = ""
        elif isinstance(value, str) and not value.strip():
            continue
        else:
            result[key] = validate_secret_value(value)
    return result


def serialize_secrets(values: dict[str, str]) -> str:
    """Strict dotenv serialization of canonical secret values.

    Explicitly deleted keys serialize as canonical ``KEY=`` tombstones.
    """
    return "".join(f"{key}={value}\n" for key, value in sorted(values.items()))


def managed_secret(name: str) -> str:
    """Resolve one managed provider credential.

    Once setup is committed, the secrets file is authoritative: a key that is
    absent or explicitly deleted (``KEY=`` tombstone) is unavailable and never
    falls back to the process environment. Before activation the environment
    may provide credentials (demo/CI deployments).
    """
    live = _read_secrets()
    value = live.get(name, "")
    if value:
        return value
    if setup_complete():
        return ""
    return os.environ.get(name, "")


def _live_paths() -> dict[str, Path]:
    return {
        "auth.json": STATE_DIR / "auth.json",
        "operator.yaml": OPERATOR_CONFIG,
        "secrets.env": SECRETS_FILE,
    }


def _commit_state(payload: dict[str, str]) -> int:
    from setup_state import CommitDurabilityError

    def _candidate_validator():
        import config as config_module

        return getattr(config_module, "validate_candidate", None)

    try:
        return commit_setup(
            STATE_DIR,
            STATE_DIR / "activated.json",
            _live_paths(),
            payload,
            validate_candidate=_candidate_validator(),
        ).version
    except CommitDurabilityError as exc:
        raise HTTPException(
            500,
            "The operator settings were saved but their durability could not be "
            "confirmed; verify storage health before continuing",
        ) from exc


def _read_secrets() -> dict[str, str]:
    if not SECRETS_FILE.exists():
        return {}
    try:
        return parse_secrets_file(SECRETS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def active_model(config: dict | Mapping | None = None) -> str:
    """The single active model slug (mirrors orchestrator resolution).

    Only ``llm.models.default`` is read at runtime; per-processor selectors
    and legacy ``llm.default_model`` are unsupported (setup_state promotes
    any legacy default during migration).
    """
    llm = (config or load_config()).get("llm", {})
    llm = llm if isinstance(llm, Mapping) else {}
    models = llm.get("models", {})
    models = models if isinstance(models, Mapping) else {}
    default = models.get("default")
    return default.strip() if isinstance(default, str) and default.strip() else ""


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
    models = llm.get("models") if isinstance(llm.get("models"), dict) else {}
    candidates = [models.get("default"), llm.get("default_model")]
    candidates.extend(
        value for key, value in models.items() if key != "default"
    )
    default_model = next(
        (
            str(candidate).strip()
            for candidate in candidates
            if isinstance(candidate, str) and candidate.strip()
        ),
        "",
    )
    if not default_model or len(default_model) > 200:
        raise HTTPException(422, "A valid default model is required")
    try:
        daily_budget = float(body.get("daily_budget_usd"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Daily budget must be numeric")
    if not 0 <= daily_budget <= 1000:
        raise HTTPException(422, "Daily budget must be between 0 and 1000")
    update = {
        "llm": {"models": {"default": default_model}},
        "budgets": {"daily_llm_usd": daily_budget},
    }
    secrets_update = (
        body.get("secrets") if isinstance(body.get("secrets"), dict) else {}
    )
    version, migrated_fields = _save_operator_state(update, secrets_update)
    restart_required = _reload_or_restart()
    response = {
        "saved": True,
        "applies_to_next_run": True,
        "model": default_model,
        "restart_required": restart_required,
    }
    if migrated_fields:
        response["legacy_profile_migrated"] = True
        response["legacy_migration"] = migrated_fields
    return response


def _reload_or_restart() -> bool:
    """Reload the configuration after a commit; truthfully report whether a
    process restart is needed (reload failure means the running process may be
    serving a stale configuration)."""
    try:
        reload_config()
        return _restart_required()
    except Exception:
        import logging

        logging.getLogger(__name__).exception("configuration reload failed")
        return True


def _save_operator_state(update: dict, secrets_update: dict) -> tuple[int, list[str]]:
    """Merge and commit a profile/secret update as a versioned snapshot.

    The existing operator profile is preserved (coverage, watchlist, timezone
    and other sections are not clobbered); a legacy base profile is migrated
    explicitly (the changed fields are returned for reporting, never silently).
    Secrets follow explicit set/unchanged/delete semantics, and the commit is
    a single atomic pointer swap. A failed commit leaves the prior live state
    untouched.
    """
    with setup_lock(STATE_DIR):
        base = read_live_state(STATE_DIR, STATE_DIR / "activated.json", _live_paths())
        try:
            base_profile = (
                yaml.safe_load(base.get("operator.yaml", "")) or {}
                if base.get("operator.yaml")
                else {}
            )
        except yaml.YAMLError:
            base_profile = {}
        if not isinstance(base_profile, dict):
            base_profile = {}
        base_profile, migrated_fields = migrate_legacy_profile(base_profile)
        profile = merge_profile(base_profile, update)
        current_secrets = (
            parse_secrets_file(base["secrets.env"]) if base.get("secrets.env") else {}
        )
        try:
            secrets = apply_secret_updates(current_secrets, secrets_update)
        except (KeyError, ValueError) as exc:
            raise HTTPException(422, f"Invalid secret update: {exc}") from exc
        payload = {
            "auth.json": base.get("auth.json", ""),
            "operator.yaml": yaml.safe_dump(profile, sort_keys=False),
            "secrets.env": serialize_secrets(secrets),
        }
        if not payload["auth.json"]:
            raise HTTPException(409, "Setup is not complete")
        return _commit_state(payload), migrated_fields


def _restart_required() -> bool:
    import config as config_module

    return bool(getattr(config_module, "restart_required", lambda: False)())


@router.get("/settings/version")
def get_config_version():
    """Expose the active versioned configuration snapshot and reload state.

    ``version`` is a content-derived digest of the validated configuration;
    ``restart_required`` is truthful for the latest reload (True only when a
    restart-sensitive section changed or a reload candidate was rejected);
    ``last_reload`` describes any rejected candidate.
    """
    return config_status()


@router.post("/settings/test-openrouter")
def test_openrouter(body: dict):
    supplied = str(body.get("api_key") or "").strip()
    api_key = supplied or managed_secret("OPENROUTER_API_KEY")
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


@router.post("/settings/test-model")
async def test_model(request: Request, body: dict | None = Body(default=None)):
    """Preflight the active (or requested) model slug without paid inference."""
    from routes.json.triggers import _internal_basic_auth

    requested = None
    if isinstance(body, dict) and isinstance(body.get("model"), str):
        requested = body.get("model").strip() or None
    try:
        auth = _internal_basic_auth()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Internal authentication unavailable"
        ) from exc
    payload = {"model": requested} if requested else {}
    base_url = orchestrator_url()
    client = getattr(request.app.state, "orchestrator_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Orchestrator client unavailable")
    try:
        response = await client.post(
            f"{base_url}/model/preflight", json=payload, auth=auth
        )
    except httpx.TransportError:
        raise HTTPException(503, "Orchestrator unavailable") from None
    except (AttributeError, TypeError):
        # Never re-send a POST on a fallback client: fail closed instead.
        raise HTTPException(503, "Orchestrator client unavailable") from None
    if response.status_code != 200:
        raise HTTPException(502, "Model preflight could not be completed")
    result = response.json()
    if not isinstance(result, dict):
        raise HTTPException(502, "Model preflight returned an invalid payload")
    return result
