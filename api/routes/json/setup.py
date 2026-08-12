import base64
import json
import math
import os
import secrets
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator

from auth import (
    ACTIVATION_FILE,
    AUTH_FILE,
    OPERATOR_FILE,
    STATE_DIR,
    LoginRateLimited,
    hash_password,
    mint_csrf_token,
    setup_complete,
    verify_login_password,
)
from routes.json.settings import (
    _read_secrets,
    _reload_or_restart,
    apply_secret_updates,
    managed_secret,
    serialize_secrets,
)
from setup_state import (
    CommitDurabilityError,
    commit_setup,
    merge_profile,
    migrate_legacy_profile,
    parse_secrets_file,
    pointer_version,
    read_live_state,
    read_pointer,
    setup_lock,
)

router = APIRouter()

COVERAGE_SOURCES = (
    "fred",
    "forex_factory",
    "cftc",
    "oecd",
    "central_banks",
    "ecb",
    "boe",
    "eia",
    "oanda",
)
DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LLM_MODEL = "deepseek/deepseek-v4-flash"

# The runtime LLM client (orchestrator/llm_client.py) posts to the fixed
# canonical OpenRouter endpoint; the setup connection test therefore only
# ever validates against this canonical origin. No custom/base_url provider
# support exists at runtime, so none is exposed here (avoids an inert SSRF
# and credential surface).

_PROFILE_DEPTH_LIMIT = 10
_PROFILE_SIZE_LIMIT = 65536
_PASSWORD_MIN_LENGTH = 12
_SETUP_TOKEN_MIN_LENGTH = 32
_SETUP_TOKEN_MAX_LENGTH = 256
_SETUP_TOKEN_PLACEHOLDER_FRAGMENTS = (
    "change-me",
    "changeme",
    "replace",
    "changethis",
    "placeholder",
    "your-token",
    "your-secret",
    "example",
)


# --------------------------------------------------------------------------
# Request models (strict: typed, no silent coercion, no unknown fields)
# --------------------------------------------------------------------------


# Request-boundary limits: attacker-controlled strings are bounded so
# scrypt/compare/parse cannot be driven into unbounded work or memory.
_STRING_FIELD_MAX = {
    "password": 1024,
    "token": 256,
    "base_url": 2048,
    "api_key": 4096,
    "provider": 64,
}


def _require_str(value: Any, field: str) -> Any:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    limit = _STRING_FIELD_MAX.get(field)
    if isinstance(value, str) and limit is not None and len(value) > limit:
        raise ValueError(f"{field} exceeds the maximum length")
    return value


def _check_coverage_value(value: Any) -> Any:
    if value is None:
        return value
    if not isinstance(value, dict):
        raise ValueError("coverage must be an object")
    for key, enabled in value.items():
        if not isinstance(key, str) or key not in COVERAGE_SOURCES:
            raise ValueError(f"unknown coverage source: {key!r}")
        if not isinstance(enabled, bool):
            raise ValueError(f"coverage source {key!r} must be a boolean")
    return value


def _check_secrets_value(value: Any) -> Any:
    if value is None:
        return value
    if not isinstance(value, dict):
        raise ValueError("secrets must be an object")
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("secret names must be strings")
        if item is not None and not isinstance(item, str):
            raise ValueError(f"secret {key!r} must be a string or null")
    return value


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: Any = None
    token: Any = None
    profile: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None
    secrets: dict[str, Any] | None = None

    @field_validator("password", "token")
    @classmethod
    def _check_str(cls, value: Any, info: Any) -> Any:
        return _require_str(value, info.field_name)

    @field_validator("profile")
    @classmethod
    def _check_profile(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, dict):
            raise ValueError("profile must be an object")
        return value

    @field_validator("coverage")
    @classmethod
    def _check_coverage(cls, value: Any) -> Any:
        return _check_coverage_value(value)

    @field_validator("secrets")
    @classmethod
    def _check_secrets(cls, value: Any) -> Any:
        return _check_secrets_value(value)


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None
    secrets: dict[str, Any] | None = None

    @field_validator("profile")
    @classmethod
    def _check_profile(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, dict):
            raise ValueError("profile must be an object")
        return value

    @field_validator("coverage")
    @classmethod
    def _check_coverage(cls, value: Any) -> Any:
        return _check_coverage_value(value)

    @field_validator("secrets")
    @classmethod
    def _check_secrets(cls, value: Any) -> Any:
        return _check_secrets_value(value)


class TestConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: Any = None
    token: Any = None

    @field_validator("api_key", "token")
    @classmethod
    def _check_str(cls, value: Any, info: Any) -> Any:
        return _require_str(value, info.field_name)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: Any = None

    @field_validator("password")
    @classmethod
    def _check_str(cls, value: Any, info: Any) -> Any:
        return _require_str(value, info.field_name)


# --------------------------------------------------------------------------
# Profile validation
# --------------------------------------------------------------------------


def _valid_profile_value(value: Any, depth: int) -> bool:
    if depth > _PROFILE_DEPTH_LIMIT:
        return False
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_valid_profile_value(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _valid_profile_value(item, depth + 1)
            for key, item in value.items()
        )
    return False


def _validate_profile(profile: dict[str, Any]) -> None:
    """Raise ``ValueError`` for profiles with unsupported or oversized content."""
    if not _valid_profile_value(profile, 0):
        raise ValueError("profile contains unsupported values or is too deeply nested")
    try:
        size = len(json.dumps(profile))
    except (TypeError, ValueError):
        raise ValueError("profile is not JSON-serializable") from None
    if size > _PROFILE_SIZE_LIMIT:
        raise ValueError("profile is too large")


def _valid_http_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and not parsed.username
        and not parsed.password
    )


# --------------------------------------------------------------------------
# Bootstrap token (production bootstrap authentication boundary)
# --------------------------------------------------------------------------


def _token_is_placeholder(token: str) -> bool:
    lowered = token.lower()
    return any(fragment in lowered for fragment in _SETUP_TOKEN_PLACEHOLDER_FRAGMENTS)


def _token_entropy_at_least_256(token: str) -> bool:
    """True when the token encodes a defensible secret (>= 256 bits).

    Fast path: a base64url secret in the generated format
    (``secrets.token_urlsafe(48)``, 64 chars) decodes to >= 32 bytes with
    real character diversity. Otherwise the estimated entropy floor
    ``length * log2(distinct characters)`` must reach 256 bits, so low-
    diversity patterns like ``'ab' * 16`` are rejected even at length 32.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        if len(decoded) >= 32 and len(set(token)) >= 16:
            return True
    except (ValueError, TypeError):
        pass
    distinct = len(set(token))
    if distinct < 2:
        return False
    return len(token) * math.log2(distinct) >= 256.0


def _valid_bootstrap_token(token: str) -> bool:
    if len(token) < _SETUP_TOKEN_MIN_LENGTH:
        return False
    if len(token) > _SETUP_TOKEN_MAX_LENGTH:
        return False
    if _token_is_placeholder(token):
        return False
    return _token_entropy_at_least_256(token)


def _require_bootstrap_token(supplied: str | None) -> None:
    """Enforce the setup token when the deployment requires it.

    In demo/test deployments the token is optional (a configured token still
    gates activation). In any other deployment (production is the default) a
    strong, non-placeholder SETUP_TOKEN is mandatory while setup is
    incomplete, and the request must present a matching value. The configured
    token is bounded by the same limits as the request field so a valid
    configuration can always be submitted.
    """
    from auth import is_demo_or_test

    configured = os.environ.get("SETUP_TOKEN", "")
    if configured:
        if not _valid_bootstrap_token(configured):
            raise HTTPException(
                503,
                "SETUP_TOKEN is configured but does not meet the strength "
                "requirements (a 64+ character high-entropy secret such as "
                "`secrets.token_urlsafe(48)`; no placeholders or repeated "
                "patterns)",
            )
        if not supplied or not secrets.compare_digest(supplied, configured):
            raise HTTPException(403, "A valid setup token is required")
        return
    if not is_demo_or_test():
        raise HTTPException(
            503,
            "SETUP_TOKEN is required to activate this deployment; set a strong "
            "token in the environment before activation",
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _coverage_config(selection: dict | None) -> dict:
    selection = selection or {}
    return {
        source_id: {"enabled": bool(selection.get(source_id, False))}
        for source_id in COVERAGE_SOURCES
    }


_merge_profile = merge_profile


def _live_paths() -> dict[str, Path]:
    return {
        "auth.json": AUTH_FILE,
        "operator.yaml": OPERATOR_FILE,
        "secrets.env": STATE_DIR / "secrets.env",
    }


def _candidate_validator():
    """Full-configuration gate for staged operator/secrets candidates.

    Delegates to ``config.validate_candidate`` when available (RuntimeConfig);
    runs under the setup lock against the staged contents before the atomic
    pointer swap, so an incompatible candidate never becomes current.
    """
    import config as config_module

    return getattr(config_module, "validate_candidate", None)


def _setup_session(request: Request) -> str:
    request.session["authenticated"] = True
    request.session["issued_at"] = int(datetime.now(UTC).timestamp())
    # Double-submit CSRF: return the exact token the csrf_contract middleware
    # minted (body token == csrf-token cookie on the same response). The
    # middleware guarantees request.state.csrf_token for the token-exempt
    # login/activation paths; a fresh token is minted only for direct
    # (non-middleware) callers such as unit tests.
    state = getattr(request, "state", None)
    return getattr(state, "csrf_token", None) or mint_csrf_token()


@router.get("/setup/status")
def status():
    return {
        "setup_complete": setup_complete(),
        "demo_available": True,
        "version": pointer_version(read_pointer(ACTIVATION_FILE)),
    }


@router.post("/setup/activate")
def activate(body: ActivationRequest, request: Request):
    if setup_complete():
        raise HTTPException(409, "Setup is locked")
    _require_bootstrap_token(body.token)
    password = body.password or ""
    if len(password) < _PASSWORD_MIN_LENGTH:
        raise HTTPException(
            400, "Password must contain at least 12 characters"
        )
    profile = dict(body.profile or {})
    try:
        _validate_profile(profile)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid profile: {exc}") from exc
    profile["collectors"] = _coverage_config(body.coverage or {})
    try:
        secrets_values = apply_secret_updates({}, body.secrets or {})
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, f"Invalid secret update: {exc}") from exc

    with setup_lock(STATE_DIR):
        if setup_complete():
            raise HTTPException(409, "Setup is locked")
        try:
            result = commit_setup(
                STATE_DIR,
                ACTIVATION_FILE,
                _live_paths(),
                {
                    "auth.json": json.dumps(hash_password(password)),
                    "operator.yaml": yaml.safe_dump(profile, sort_keys=False),
                    "secrets.env": serialize_secrets(secrets_values),
                },
                validate_candidate=_candidate_validator(),
            )
        except ValueError as exc:
            raise HTTPException(
                422, f"Setup could not be activated: {exc}"
            ) from exc
        except CommitDurabilityError as exc:
            raise HTTPException(
                500,
                "Setup was activated but its durability could not be confirmed; "
                "verify storage health before continuing",
            ) from exc
        except OSError as exc:
            raise HTTPException(
                500, "Setup could not be activated; you can safely retry"
            ) from exc
    restart_required = _reload_or_restart()
    return {
        "activated": True,
        "csrf_token": _setup_session(request),
        "version": result.version,
        "restart_required": restart_required,
    }


@router.put("/setup/profile")
def update_profile(body: ProfileUpdateRequest, request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(401, "Login required")
    profile_update = body.profile or {}
    try:
        _validate_profile(profile_update)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid profile: {exc}") from exc
    with setup_lock(STATE_DIR):
        base = read_live_state(STATE_DIR, ACTIVATION_FILE, _live_paths())
        try:
            existing = yaml.safe_load(base.get("operator.yaml", "")) or {}
        except yaml.YAMLError:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        # One-time, explicitly reported migration of a legacy base profile
        # (promotes llm.default_model, drops inert legacy keys); the user's
        # submitted fields are never modified and unsupported submissions
        # fail the strict staged validation with 422.
        existing, migrated_fields = migrate_legacy_profile(existing)
        if "coverage" in body.model_fields_set:
            profile_update = dict(profile_update)
            profile_update["collectors"] = _coverage_config(body.coverage or {})
        profile = merge_profile(existing, profile_update)
        current_secrets = (
            parse_secrets_file(base["secrets.env"]) if base.get("secrets.env") else {}
        )
        try:
            secrets_values = apply_secret_updates(current_secrets, body.secrets or {})
        except (KeyError, ValueError) as exc:
            raise HTTPException(422, f"Invalid secret update: {exc}") from exc
        if not base.get("auth.json"):
            raise HTTPException(409, "Setup is not complete")
        try:
            result = commit_setup(
                STATE_DIR,
                ACTIVATION_FILE,
                _live_paths(),
                {
                    "auth.json": base["auth.json"],
                    "operator.yaml": yaml.safe_dump(profile, sort_keys=False),
                    "secrets.env": serialize_secrets(secrets_values),
                },
                validate_candidate=_candidate_validator(),
            )
        except ValueError as exc:
            raise HTTPException(422, f"Profile could not be saved: {exc}") from exc
        except CommitDurabilityError as exc:
            raise HTTPException(
                500,
                "The profile was saved but its durability could not be confirmed; "
                "verify storage health before continuing",
            ) from exc
        except OSError as exc:
            raise HTTPException(
                500, "Profile could not be saved; the previous settings remain active"
            ) from exc
    restart_required = _reload_or_restart()
    response = {
        "saved": True,
        "restart_required": restart_required,
        "version": result.version,
    }
    if migrated_fields:
        response["legacy_profile_migrated"] = True
        response["legacy_migration"] = migrated_fields
    return response


@router.post("/setup/test-connection")
def test_connection(body: TestConnectionRequest, request: Request):
    if not request.session.get("authenticated"):
        if setup_complete():
            raise HTTPException(401, "Login required")
        # Pre-activation this endpoint is an unauthenticated outbound probe:
        # the bootstrap token gates it so it cannot be abused publicly.
        _require_bootstrap_token(body.token)

    from contracts.outbound_security import (
        OutboundSecurityError,
        parse_origin,
        resolve_redirect_url,
        validate_public_url,
    )
    from outbound import PublicOnlyTransport

    # The runtime LLM client posts to the fixed canonical OpenRouter origin
    # only; the test validates exactly that origin (no custom/base_url input
    # exists, so there is no arbitrary-host credential surface).
    base_url = DEFAULT_LLM_BASE_URL
    live = _read_secrets()
    if "OPENROUTER_API_KEY" in live:
        # Present (possibly a KEY= deletion tombstone) is authoritative:
        # an empty value means explicitly deleted and is never re-sourced.
        api_key = (body.api_key or "").strip() or live["OPENROUTER_API_KEY"]
    else:
        # Legacy alias, or the environment before activation (demo/CI);
        # managed_secret fails closed once setup is committed.
        api_key = (
            (body.api_key or "").strip()
            or managed_secret("OPENROUTER_API_KEY")
            or managed_secret("LLM_API_KEY")
        )

    if not api_key:
        # A deleted (tombstoned) or absent managed key is unavailable; never
        # probe with an empty credential or resurrect the process environment.
        raise HTTPException(400, "Add an API key before testing the connection")

    # The endpoint is reachable with only the setup token, so it is a classic
    # SSRF probe surface: the canonical origin must resolve to globally
    # routable addresses and the connection is pinned to a validated address.
    try:
        validate_public_url(f"{base_url}/models")
    except OutboundSecurityError as exc:
        raise HTTPException(
            400, f"Provider endpoint is not a public URL ({exc})"
        ) from exc
    origin = parse_origin(base_url)
    try:
        with httpx.Client(
            transport=PublicOnlyTransport(), timeout=10, follow_redirects=False
        ) as client:
            current_url = f"{base_url}/models"
            response = client.get(
                current_url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            # Manual redirects: every hop is re-validated by the pinned
            # transport and resolved against the CURRENT url (relative
            # Locations accumulate), a cross-origin hop is rejected outright
            # so the Authorization header is never forwarded off the
            # canonical origin, and redirect-limit exhaustion/loops fail
            # closed instead of reporting a connection.
            hops = 0
            while response.status_code in {301, 302, 303, 307, 308}:
                hops += 1
                if hops > 3:
                    raise HTTPException(
                        400, "Provider redirected too many times"
                    )
                location = response.headers.get("location")
                if not location:
                    raise HTTPException(
                        400, "Provider redirected without a location"
                    )
                try:
                    target = resolve_redirect_url(current_url, location)
                    target_origin = parse_origin(target)
                except OutboundSecurityError as exc:
                    raise HTTPException(
                        400, f"Provider redirect is not a public URL ({exc})"
                    ) from exc
                if (target_origin.scheme, target_origin.host, target_origin.port) != (
                    origin.scheme,
                    origin.host,
                    origin.port,
                ):
                    raise HTTPException(
                        400, "Provider redirects across origins are not allowed"
                    )
                current_url = target
                response = client.get(
                    current_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        response.raise_for_status()
    except OutboundSecurityError as exc:
        raise HTTPException(
            400, f"Provider endpoint is not a public URL ({exc})"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            400, f"Provider rejected the connection ({exc.response.status_code})"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(400, "Could not reach the provider endpoint") from exc
    return {"connected": True}


@router.post("/login")
def login(body: LoginRequest, request: Request):
    if not setup_complete():
        raise HTTPException(409, "Setup not complete")
    try:
        record = json.loads(AUTH_FILE.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        raise HTTPException(503, "Setup state is unavailable") from None
    client_host = request.client.host if request.client is not None else "unknown"
    try:
        valid = bool(
            isinstance(record, dict)
            and isinstance(record.get("salt"), str)
            and isinstance(record.get("hash"), str)
            and verify_login_password(
                body.password or "",
                record,
                client_host,
            )
        )
    except LoginRateLimited as exc:
        raise HTTPException(
            429,
            "Too many login attempts",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise HTTPException(401, "Invalid credentials")
    return {"authenticated": True, "csrf_token": _setup_session(request)}


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"authenticated": False}
