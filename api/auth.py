import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
import secrets
import shutil
import threading
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic(auto_error=False)
CSRF_COOKIE = "csrf-token"
SSE_PURPOSE = "quotes-stream"
CSRF_PURPOSE = "csrf"

# Unsafe-method routes that may run without a CSRF token. Exact paths only:
# these are the pre-authentication bootstrap endpoints (login and setup
# activation). Everything else - including /api/setup/profile and
# /api/setup/test-connection - requires a valid token once authenticated.
CSRF_TOKEN_EXEMPT_PATHS = frozenset({"/api/login", "/api/setup/activate"})

DEPLOYMENT_MODE_VAR = "DEPLOYMENT_MODE"
DISABLE_AUTH_VAR = "DISABLE_AUTH"
SESSION_SIGNING_KEY_VAR = "SESSION_SIGNING_KEY"
SESSION_SIGNING_KEY_PREVIOUS_VAR = "SESSION_SIGNING_KEY_PREVIOUS"
CSRF_SIGNING_KEY_VAR = "CSRF_SIGNING_KEY"
SSE_SIGNING_KEY_VAR = "SSE_SIGNING_KEY"
EXTERNAL_ORIGIN_VAR = "EXTERNAL_ORIGIN"
TRUSTED_HOSTS_VAR = "TRUSTED_HOSTS"
DASHBOARD_USER_VAR = "DASHBOARD_USER"
DASHBOARD_PASSWORD_VAR = "DASHBOARD_PASSWORD"
SESSION_MAX_AGE_VAR = "SESSION_MAX_AGE_SECONDS"
COOKIE_SECURE_VAR = "COOKIE_SECURE"

_MIN_SESSION_MAX_AGE = 1
_MAX_SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days

_DEMO_OR_TEST_MODES = frozenset({"demo", "test"})
_MIN_SIGNING_KEY_LENGTH = 43  # base64url-encoded 256-bit floor
_MIN_SIGNING_KEY_BYTES = 32
_MIN_SIGNING_KEY_DISTINCT_CHARS = 16
_PLACEHOLDER_PREFIXES = (
    "replace-with-",
    "replace_with_",
    "replaceme",
    "changeme",
    "change-me",
    "change_me",
    "your-key",
    "your_secret",
    "your-secret",
    "sample-",
    "sample_",
    "example-",
    "example_",
    "demo-",
    "demo_",
    "test-",
    "test_",
    "secret-",
    "secret_",
    "dev-",
    "dev_",
    "development-",
)
_LOGIN_WINDOW_SECONDS = 60.0
_LOGIN_MAX_ATTEMPTS = 10
_LOGIN_MAX_TRACKED_CLIENTS = 4096
_login_hash_slots = threading.BoundedSemaphore(value=2)
_login_attempts_lock = threading.Lock()
_login_attempts: OrderedDict[str, deque[float]] = OrderedDict()


class LoginRateLimited(RuntimeError):
    """Login hashing budget exhausted before expensive password verification."""

    def __init__(self, retry_after: int):
        super().__init__("Login attempt budget exhausted")
        self.retry_after = max(1, retry_after)


_SIGNING_KEY_PLACEHOLDERS = frozenset(
    value.strip().lower()
    for value in (
        "changeme",
        "change-me",
        "change_me",
        "changeme123",
        "replaceme",
        "replace-me",
        "replace_me",
        "placeholder",
        "your-key",
        "your-key-here",
        "your-secret",
        "your_secret",
        "your-secret-key",
        "secret",
        "secret-key",
        "password",
        "demo",
        "demo-key",
        "demo-secret",
        "test",
        "test-key",
        "test-secret",
        "development",
        "development-key",
        "dev",
        "example",
        "example-key",
        "sample",
        "sample-key",
        "0" * 64,
    )
)

STATE_DIR = Path(os.environ.get("STATE_DIR", "/app/state"))
AUTH_FILE = STATE_DIR / "auth.json"
ACTIVATION_FILE = STATE_DIR / "activated.json"
OPERATOR_FILE = STATE_DIR / "operator.yaml"
SECRETS_FILE = STATE_DIR / "secrets.env"
SESSION_SECRET_FILE = STATE_DIR / "session_secret"
STATE_FILENAMES = (
    "auth.json",
    "operator.yaml",
    "secrets.env",
    "activated.json",
    "session_secret",
)


def is_html_request(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return (
        request.method in {"GET", "HEAD"}
        and not request.url.path.startswith("/api/")
        and "text/html" in accept
    )


def login_redirect(request: Request) -> HTTPException:
    destination = request.url.path
    if request.url.query:
        destination += f"?{request.url.query}"
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"/login?next={quote(destination, safe='/')}"},
    )


def deployment_mode() -> str:
    """Canonical deployment mode; production is the default and fail-closed."""
    raw = os.environ.get(DEPLOYMENT_MODE_VAR, "production") or "production"
    return raw.strip().lower() or "production"


def is_demo_or_test() -> bool:
    return deployment_mode() in _DEMO_OR_TEST_MODES


def auth_disabled() -> bool:
    """Fail-closed: DISABLE_AUTH only takes effect in demo or test deployments."""
    raw = os.environ.get(DISABLE_AUTH_VAR, "")
    return raw.lower() in {"1", "true", "yes"} and is_demo_or_test()


def legacy_basic_auth_enabled() -> bool:
    """True when the operator opted into the legacy HTTP Basic credential path."""
    raw = os.environ.get("LEGACY_BASIC_AUTH", "")
    return raw.lower() in {"1", "true", "yes"}


def demo_basic_auth_configured() -> bool:
    """True when this demo/test deployment authenticates through configured
    HTTP Basic credentials (LEGACY_BASIC_AUTH plus both DASHBOARD_* vars).

    Demo bootstrap: a fresh volume has no committed setup state, and the demo
    Compose file supplies non-secret demo credentials instead of the setup
    form. When they are configured, the browser receives the native Basic
    challenge at the root (sign in with demo/demo) and never the setup
    bootstrap. This is explicitly demo/test-only; production (the default)
    keeps the fail-closed setup bootstrap even when legacy auth is enabled.
    """
    return (
        is_demo_or_test()
        and legacy_basic_auth_enabled()
        and bool(os.environ.get(DASHBOARD_USER_VAR, ""))
        and bool(os.environ.get(DASHBOARD_PASSWORD_VAR, ""))
    )


def verify_credentials(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> str:
    if auth_disabled():
        return "admin"
    public_paths = {
        "/setup",
        "/api/setup/status",
        "/api/setup/activate",
        "/api/setup/test-connection",
        "/login",
        "/api/login",
        "/api/meta/build",
        "/api/quotes/stream",
        "/ready",
        "/live",
    }
    if request.url.path in public_paths or request.url.path.startswith("/static/"):
        return "bootstrap"
    if request.url.path == "/" and not setup_complete():
        # A fresh root shows the setup bootstrap only when no demo credentials
        # are configured; demo deployments challenge HTTP Basic instead so a
        # brand-new volume can sign in with demo/demo and never sees /setup.
        if not demo_basic_auth_configured():
            return "bootstrap"
    session = (
        request.scope.get("session", {})
        if hasattr(request, "scope")
        else getattr(request, "session", {})
    )
    if session.get("authenticated"):
        return "admin"
    if setup_complete():
        if is_html_request(request):
            raise login_redirect(request)
        raise HTTPException(status_code=401, detail="Login required")

    expected_user = os.environ.get("DASHBOARD_USER", "")
    expected_pass = os.environ.get("DASHBOARD_PASSWORD", "")
    legacy_enabled = legacy_basic_auth_enabled()
    if legacy_enabled and credentials and expected_user and expected_pass:
        if secrets.compare_digest(
            credentials.username, expected_user
        ) and secrets.compare_digest(credentials.password, expected_pass):
            return credentials.username
    if is_html_request(request) and not (
        request.url.path == "/" and demo_basic_auth_configured()
    ):
        raise login_redirect(request)
    headers = {"WWW-Authenticate": "Basic"} if legacy_enabled else None
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Setup required" if not legacy_enabled else "Invalid credentials",
        headers=headers,
    )


def _try_decode_key(value: str) -> bytes | None:
    """Decode a base64url or hex key; None when the value is neither."""
    try:
        return base64.urlsafe_b64decode(
            value + "=" * (-len(value) % 4), validate=True
        )
    except (ValueError, TypeError):
        pass
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None


def _signing_key_problem(raw: str) -> str | None:
    """Return a short problem label for an unacceptable key, else None.

    Generated secrets must be single tokens (no whitespace - prose passphrases
    are not signing keys) with a defensible entropy floor: base64url/hex keys
    must decode to at least 32 bytes and any key must use at least 16 distinct
    characters and (for non-encoded alphabets) at least 43 characters. Small
    repeated alphabets such as ``'abcd' * 8`` are rejected.
    """
    value = raw.strip()
    lowered = value.lower()
    if not value:
        return "empty"
    if lowered in _SIGNING_KEY_PLACEHOLDERS or lowered.startswith(
        _PLACEHOLDER_PREFIXES
    ):
        return "placeholder"
    if any(char.isspace() for char in value):
        return "low-entropy"
    decoded = _try_decode_key(value)
    if decoded is not None:
        if len(decoded) < _MIN_SIGNING_KEY_BYTES:
            return "low-entropy"
    elif len(value) < _MIN_SIGNING_KEY_LENGTH:
        return "low-entropy"
    if len(set(value)) < _MIN_SIGNING_KEY_DISTINCT_CHARS:
        return "low-entropy"
    return None


def _resolve_env_signing_key(var: str) -> bytes | None:
    """Resolve a signing key from the environment.

    Returns None when the variable is unset or empty. Outside demo/test a
    placeholder, low-entropy, or empty configured value is a startup error
    (fail closed); demo/test deployments may use weak keys for convenience.
    """
    raw = os.environ.get(var, "")
    if not raw:
        return None
    problem = _signing_key_problem(raw)
    if problem is not None and not is_demo_or_test():
        raise RuntimeError(
            f"{var} is {problem}; refusing to run with it in {deployment_mode()} mode"
        )
    return raw.encode()


_EPHEMERAL_KEYS: dict[str, bytes] = {}


def _ephemeral_signing_key(var: str) -> bytes:
    """Process-stable random key for demo/test deployments without config."""
    key = _EPHEMERAL_KEYS.get(var)
    if key is None:
        key = secrets.token_bytes(32)
        _EPHEMERAL_KEYS[var] = key
    return key


def csrf_secret() -> bytes:
    """Signing key for CSRF tokens: CSRF_SIGNING_KEY (ephemeral in demo/test)."""
    key = _resolve_env_signing_key(CSRF_SIGNING_KEY_VAR)
    if key is not None:
        return key
    if is_demo_or_test():
        return _ephemeral_signing_key(CSRF_SIGNING_KEY_VAR)
    raise RuntimeError(
        f"{CSRF_SIGNING_KEY_VAR} is not set; refusing to run "
        f"in {deployment_mode()} mode"
    )


def sse_secret() -> bytes:
    """Signing key for SSE tokens: SSE_SIGNING_KEY (ephemeral in demo/test)."""
    key = _resolve_env_signing_key(SSE_SIGNING_KEY_VAR)
    if key is not None:
        return key
    if is_demo_or_test():
        return _ephemeral_signing_key(SSE_SIGNING_KEY_VAR)
    raise RuntimeError(
        f"{SSE_SIGNING_KEY_VAR} is not set; refusing to run in {deployment_mode()} mode"
    )


def _sign_payload(secret: bytes, payload: dict) -> str:
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _verify_payload(secret: bytes, token: str) -> dict | None:
    """Verify an HMAC signature and decode the payload; None on any failure."""
    try:
        encoded, supplied = token.split(".", 1)
        expected = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(payload, dict):
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, OverflowError):
        return None


def mint_sse_token(path: str = "/api/quotes/stream", ttl: int = 60) -> str:
    payload = {
        "path": path,
        "purpose": SSE_PURPOSE,
        "exp": int(time.time()) + ttl,
        "jti": uuid.uuid4().hex,
    }
    return _sign_payload(sse_secret(), payload)


def mint_csrf_token(ttl: int = 3600) -> str:
    return _sign_payload(
        csrf_secret(),
        {
            "purpose": CSRF_PURPOSE,
            "exp": int(time.time()) + ttl,
            "jti": uuid.uuid4().hex,
        },
    )


def _token_not_expired(payload: dict) -> bool:
    try:
        return int(payload.get("exp", 0)) > int(time.time())
    except (TypeError, ValueError, OverflowError):
        return False


def verify_sse_token(token: str | None, path: str) -> bool:
    if not token:
        return False
    try:
        secret = sse_secret()
    except RuntimeError:
        return False
    payload = _verify_payload(secret, token)
    if payload is None:
        return False
    return (
        payload.get("path") == path
        and payload.get("purpose") == SSE_PURPOSE
        and _token_not_expired(payload)
    )


def verify_csrf_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        secret = csrf_secret()
    except RuntimeError:
        return False
    payload = _verify_payload(secret, token)
    if payload is None:
        return False
    return payload.get("purpose") == CSRF_PURPOSE and _token_not_expired(payload)


def sign_session_cookie(session: dict, *, secret: bytes) -> str:
    return _sign_payload(secret, session)


def decode_session_cookie(cookie: str, *, secret: bytes) -> dict:
    """Decode and verify a signed session cookie; {} on failure."""
    payload = _verify_payload(secret, cookie)
    return payload if payload is not None else {}


def hash_password(password: str) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return {
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(digest).decode(),
    }


def verify_password(password: str, record: dict) -> bool:
    salt = base64.b64decode(record["salt"])
    actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return secrets.compare_digest(base64.b64encode(actual).decode(), record["hash"])


def verify_login_password(password: str, record: dict, client_host: str) -> bool:
    """Rate-limit and concurrency-bound the anonymous memory-hard hash."""
    now = time.monotonic()
    key = (client_host or "unknown")[:255]
    with _login_attempts_lock:
        attempts = _login_attempts.get(key)
        if attempts is None:
            if len(_login_attempts) >= _LOGIN_MAX_TRACKED_CLIENTS:
                _login_attempts.popitem(last=False)
            attempts = deque()
            _login_attempts[key] = attempts
        else:
            _login_attempts.move_to_end(key)
        cutoff = now - _LOGIN_WINDOW_SECONDS
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            retry_after = math.ceil(_LOGIN_WINDOW_SECONDS - (now - attempts[0]))
            raise LoginRateLimited(retry_after)
        attempts.append(now)

    if not _login_hash_slots.acquire(blocking=False):
        raise LoginRateLimited(1)
    try:
        return verify_password(password, record)
    finally:
        _login_hash_slots.release()


def create_admin(password: str) -> None:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = AUTH_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(hash_password(password)))
    temporary.chmod(0o600)
    temporary.replace(AUTH_FILE)


def setup_complete() -> bool:
    """True only when a committed, complete setup state exists and validates.

    The activation marker is the atomic current pointer: activation and every
    profile/secret commit publish a fully validated versioned snapshot before
    flipping it, so this function never reports partial or mixed state as
    complete. Legacy (pre-versioning) states are validated from the root files.
    """
    from setup_state import validate_committed_state

    return validate_committed_state(
        state_dir=STATE_DIR,
        marker_path=ACTIVATION_FILE,
        live_paths={
            "auth.json": AUTH_FILE,
            "operator.yaml": OPERATOR_FILE,
            "secrets.env": SECRETS_FILE,
        },
    )


def load_session_secret() -> bytes:
    """Current session signing key: SESSION_SIGNING_KEY.

    In demo/test deployments an unset key falls back to the generated
    STATE_DIR/session_secret file (bootstrap). Production requires the
    environment variable; there is no generated fallback there.
    """
    key = _resolve_env_signing_key(SESSION_SIGNING_KEY_VAR)
    if key is not None:
        return key
    if is_demo_or_test():
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not SESSION_SECRET_FILE.exists():
            temporary = SESSION_SECRET_FILE.with_suffix(".tmp")
            temporary.write_text(secrets.token_urlsafe(48))
            temporary.chmod(0o600)
            temporary.replace(SESSION_SECRET_FILE)
        return SESSION_SECRET_FILE.read_text().strip().encode()
    raise RuntimeError(
        f"{SESSION_SIGNING_KEY_VAR} is not set; refusing to run "
        f"in {deployment_mode()} mode"
    )


def load_previous_session_secret() -> bytes | None:
    """Optional rotation key that may still verify previously issued sessions."""
    return _resolve_env_signing_key(SESSION_SIGNING_KEY_PREVIOUS_VAR)


def canonical_origin() -> tuple[str, str] | None:
    """Canonical external browser origin from EXTERNAL_ORIGIN.

    Returns (scheme, host[:port]) normalized for comparison, or None when unset
    (only permitted in demo/test). Production requires HTTPS except for an
    explicitly loopback-only origin. Userinfo, query strings, fragments, and
    non-root paths are rejected.
    """
    raw = os.environ.get(EXTERNAL_ORIGIN_VAR, "").strip()
    if not raw:
        if is_demo_or_test():
            return None
        raise RuntimeError(
            f"{EXTERNAL_ORIGIN_VAR} is required in {deployment_mode()} mode"
        )
    if "://" not in raw:
        raise RuntimeError(
            f"{EXTERNAL_ORIGIN_VAR} must be a full origin, "
            "e.g. https://dash.example.com"
        )
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise RuntimeError(f"{EXTERNAL_ORIGIN_VAR} is not a valid origin: {raw!r}")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError(f"{EXTERNAL_ORIGIN_VAR} must not contain userinfo: {raw!r}")
    if not parsed.hostname:
        raise RuntimeError(f"{EXTERNAL_ORIGIN_VAR} is not a valid origin: {raw!r}")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise RuntimeError(f"{EXTERNAL_ORIGIN_VAR} is not a valid origin: {raw!r}")
    if not is_demo_or_test() and parsed.scheme.lower() == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            raise RuntimeError(
                f"{EXTERNAL_ORIGIN_VAR} must use HTTPS outside loopback"
            )
    try:
        return normalize_origin(parsed)
    except ValueError as exc:
        raise RuntimeError(
            f"{EXTERNAL_ORIGIN_VAR} is not a valid origin: {raw!r}"
        ) from exc


def normalize_origin(parsed) -> tuple[str, str]:
    """Canonical (scheme, host[:port]) with default ports dropped.

    Uses the parsed hostname/port so IPv6 literals and default ports compare
    consistently; raises ValueError for malformed ports.
    """
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname or ""
    port = parsed.port
    host = hostname.lower()
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    return (scheme, host)


def expected_origin(request: Request) -> tuple[str, str]:
    """Origin browser mutations must match: EXTERNAL_ORIGIN, else request base URL."""
    canonical = canonical_origin()
    if canonical is not None:
        return canonical
    return normalize_origin(urlsplit(str(request.base_url)))


def validate_signing_keys() -> None:
    """Fail-closed startup validation of signing material outside demo/test."""
    if is_demo_or_test():
        return
    session = load_session_secret()
    csrf = csrf_secret()
    sse = sse_secret()
    load_previous_session_secret()
    if len({session, csrf, sse}) != 3:
        raise RuntimeError(
            f"{SESSION_SIGNING_KEY_VAR}, {CSRF_SIGNING_KEY_VAR} and "
            f"{SSE_SIGNING_KEY_VAR} must be distinct in {deployment_mode()} mode"
        )
    if os.environ.get(DISABLE_AUTH_VAR, "").lower() in {"1", "true", "yes"}:
        raise RuntimeError(
            f"{DISABLE_AUTH_VAR} is only accepted when {DEPLOYMENT_MODE_VAR} "
            "is demo or test"
        )
    canonical_origin()


def cookie_secure() -> bool:
    """Single parser for COOKIE_SECURE across all set-cookie sites."""
    return os.environ.get(COOKIE_SECURE_VAR, "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def validate_cookie_security() -> None:
    """Fail closed when browser transport and cookie confidentiality disagree.

    Production requires HTTPS and Secure cookies except for an explicit
    loopback HTTP origin. Demo/test deployments may use either combination.
    """
    if is_demo_or_test():
        return
    origin = canonical_origin()
    if origin is None:
        return
    scheme, _ = origin
    if scheme == "https" and not cookie_secure():
        raise RuntimeError(
            f"{EXTERNAL_ORIGIN_VAR} is HTTPS but {COOKIE_SECURE_VAR} is not "
            "enabled; session and CSRF cookies would be sent without Secure"
        )
    if scheme == "http" and cookie_secure():
        raise RuntimeError(
            f"{EXTERNAL_ORIGIN_VAR} is loopback HTTP but {COOKIE_SECURE_VAR} is "
            "enabled; browsers do not accept Secure cookies over plain HTTP"
        )


def trusted_hosts() -> list[str] | None:
    """Comma-separated TRUSTED_HOSTS entries, or None when unset."""
    raw = os.environ.get(TRUSTED_HOSTS_VAR, "")
    if not raw.strip():
        return None
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def _valid_dns_hostname(host: str) -> bool:
    if not host or len(host) > 253 or host.endswith("."):
        return False
    for label in host.split("."):
        if (
            not label
            or len(label) > 63
            or label[0] == "-"
            or label[-1] == "-"
            or not all(char.isalnum() or char == "-" for char in label)
        ):
            return False
    return True


def _valid_ipv4_host(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
    except ValueError:
        return False


def _normalize_host_entry(entry: str) -> str | None:
    """Validate one TRUSTED_HOSTS entry and return its middleware form.

    Accepted forms: exact DNS hostnames, IPv4 addresses, and safe leading
    subdomain wildcards (``*.example.com``). ``*`` alone is rejected because
    TrustedHostMiddleware treats it as allow-all; malformed wildcards, ports,
    and IPv6 literals are rejected because the middleware cannot match them
    (it strips ports by splitting the Host header on ``:``, which mangles IPv6
    addresses).
    """
    value = entry.strip()
    if not value or any(char.isspace() for char in value):
        return None
    if "://" in value or "/" in value or "@" in value:
        return None
    if value == "*":
        return None
    if "*" in value:
        if not value.startswith("*.") or "*" in value[2:]:
            return None
        suffix = value[2:]
        if not _valid_dns_hostname(suffix):
            return None
        return f"*.{suffix.lower()}"
    if ":" in value:
        return None
    if _valid_ipv4_host(value):
        return value
    if _valid_dns_hostname(value):
        return value.lower()
    return None


def validate_host_security() -> list[str] | None:
    """Fail-closed host allowlist validation outside demo/test.

    Returns the normalized allowed hosts for TrustedHostMiddleware, or None
    when the middleware should not be installed (demo/test without
    configuration). Only exact DNS hostnames, IPv4 addresses, and safe leading
    ``*.domain`` subdomain wildcards are accepted; ``*`` (which disables host
    protection), malformed wildcards, ports, and IPv6 literals are rejected.
    """
    hosts = trusted_hosts()
    if hosts is None:
        if is_demo_or_test():
            return None
        raise RuntimeError(
            f"{TRUSTED_HOSTS_VAR} is required in {deployment_mode()} mode"
        )
    allowed = []
    for entry in hosts:
        normalized = _normalize_host_entry(entry)
        if normalized is None:
            raise RuntimeError(
                f"{TRUSTED_HOSTS_VAR} contains an invalid host: {entry!r}"
            )
        allowed.append(normalized)
    return allowed


def session_max_age_seconds() -> int:
    """Parse and validate SESSION_MAX_AGE_SECONDS as a positive bounded integer."""
    raw = os.environ.get(SESSION_MAX_AGE_VAR, "43200")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{SESSION_MAX_AGE_VAR} must be an integer, got {raw!r}"
        ) from exc
    if not _MIN_SESSION_MAX_AGE <= value <= _MAX_SESSION_MAX_AGE:
        raise RuntimeError(
            f"{SESSION_MAX_AGE_VAR} must be between {_MIN_SESSION_MAX_AGE} "
            f"and {_MAX_SESSION_MAX_AGE} seconds"
        )
    return value


def validate_operator_credentials() -> None:
    """Fail-closed startup gate for internal basic-auth credentials.

    Delegates to the shared stdlib-only rule set in
    ``contracts.runtime_config`` so the API and orchestrator enforce identical
    rules: outside demo/test the DASHBOARD_USER/DASHBOARD_PASSWORD pair must be
    set, non-placeholder, and the password at least 12 characters. Error
    messages name the variables, never the values.
    """
    if is_demo_or_test():
        return
    from contracts.runtime_config import (
        validate_operator_credentials as validate_shared,
    )

    try:
        validate_shared(
            os.environ.get(DASHBOARD_USER_VAR, ""),
            os.environ.get(DASHBOARD_PASSWORD_VAR, ""),
            deployment_mode=deployment_mode(),
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def migrate_legacy_state() -> bool:
    legacy_value = os.environ.get("LEGACY_STATE_DIR", "")
    if not legacy_value:
        return False
    legacy_dir = Path(legacy_value)
    if not legacy_dir.is_dir():
        return False
    if AUTH_FILE.exists() and OPERATOR_FILE.exists():
        if ACTIVATION_FILE.exists():
            return False
        temporary = ACTIVATION_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({"migrated": True, "version": 1}))
        temporary.chmod(0o600)
        temporary.replace(ACTIVATION_FILE)
        return True
    if (STATE_DIR / "current").exists() or any(
        (STATE_DIR / name).exists() for name in STATE_FILENAMES
    ):
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied = False
    for name in STATE_FILENAMES:
        source = legacy_dir / name
        if source.exists():
            destination = STATE_DIR / name
            shutil.copy2(source, destination)
            destination.chmod(0o600)
            copied = True
    if copied:
        temporary = ACTIVATION_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({"migrated": True, "version": 1}))
        temporary.chmod(0o600)
        temporary.replace(ACTIVATION_FILE)
    return copied
