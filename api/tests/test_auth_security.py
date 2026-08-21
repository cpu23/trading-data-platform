import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

# unittest discovery imports test modules as top-level modules, so package-level
# test fixtures are not guaranteed to run before auth captures its state paths.
_TEST_STATE_DIR = tempfile.mkdtemp(prefix="trading-api-auth-tests-")
os.environ["STATE_DIR"] = _TEST_STATE_DIR
os.environ["LEGACY_STATE_DIR"] = ""
os.environ["LEGACY_BASIC_AUTH"] = "true"
os.environ["DEPLOYMENT_MODE"] = "test"
os.environ["DASHBOARD_USER"] = "test"
os.environ["DASHBOARD_PASSWORD"] = "test"
os.environ.update(
    SSE_SIGNING_KEY="test-sse-signing-key-0123456789abcdef",
    CSRF_SIGNING_KEY="test-csrf-signing-key-0123456789abcdef",
    SESSION_SIGNING_KEY="test-session-signing-key-0123456789abcdef",
)
os.environ["DEPLOYMENT_MODE"] = "test"

from types import SimpleNamespace

import auth
from routes.json import setup


class AuthSecurityTests(unittest.TestCase):
    def test_password_hash_round_trip(self):
        record = auth.hash_password("a sufficiently long password")
        self.assertTrue(auth.verify_password("a sufficiently long password", record))
        self.assertFalse(auth.verify_password("wrong password", record))

    def test_anonymous_login_hashing_is_rate_and_concurrency_bounded(self):
        client_host = "rate-limit-unit-test"
        with auth._login_attempts_lock:
            auth._login_attempts.pop(client_host, None)
        verifier = patch.object(auth, "verify_password", return_value=False)
        with patch.object(auth, "_LOGIN_MAX_ATTEMPTS", 1), verifier as verify:
            self.assertFalse(auth.verify_login_password("wrong", {}, client_host))
            with self.assertRaises(auth.LoginRateLimited) as raised:
                auth.verify_login_password("wrong", {}, client_host)
        self.assertGreaterEqual(raised.exception.retry_after, 1)
        verify.assert_called_once()

        saturated = Mock()
        saturated.acquire.return_value = False
        with (
            patch.object(auth, "_login_hash_slots", saturated),
            self.assertRaises(auth.LoginRateLimited),
        ):
            auth.verify_login_password("wrong", {}, "concurrency-unit-test")
        saturated.acquire.assert_called_once_with(blocking=False)

    def test_admin_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            with (
                patch.object(auth, "STATE_DIR", Path(directory)),
                patch.object(auth, "AUTH_FILE", path),
            ):
                auth.create_admin("a sufficiently long password")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_setup_activation_returns_client_error_for_short_password(self):
        with patch.object(setup, "setup_complete", return_value=False):
            with self.assertRaises(setup.HTTPException) as raised:
                setup.activate(setup.ActivationRequest(password="too-short"), object())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            raised.exception.detail, "Password must contain at least 12 characters"
        )

    def test_coverage_selection_maps_only_supported_collectors(self):
        config = setup._coverage_config(
            {
                "fred": True,
                "cftc": False,
                "oecd": True,
                "unknown": True,
            }
        )
        self.assertEqual(set(config), set(setup.COVERAGE_SOURCES))
        self.assertTrue(config["fred"]["enabled"])
        self.assertTrue(config["oecd"]["enabled"])
        self.assertFalse(config["cftc"]["enabled"])
        self.assertNotIn("unknown", config)

    def test_missing_coverage_disables_all_optional_sources(self):
        config = setup._coverage_config(None)
        self.assertTrue(all(not value["enabled"] for value in config.values()))

    def test_secret_update_preserves_existing_key_when_blank(self):
        values = setup.apply_secret_updates({}, {"OPENROUTER_API_KEY": "existing"})
        values = setup.apply_secret_updates(values, {"OPENROUTER_API_KEY": ""})
        self.assertEqual(values["OPENROUTER_API_KEY"], "existing")
        values = setup.apply_secret_updates(values, {"OPENROUTER_API_KEY": None})
        self.assertEqual(values["OPENROUTER_API_KEY"], "")

    def test_profile_merge_preserves_unedited_sections(self):
        merged = setup._merge_profile(
            {
                "llm": {"default_model": "old", "timeout_seconds": 90},
                "watchlist": {"trading": []},
            },
            {"llm": {"default_model": "new"}},
        )
        self.assertEqual(merged["llm"]["default_model"], "new")
        self.assertEqual(merged["llm"]["timeout_seconds"], 90)
        self.assertIn("watchlist", merged)

    def test_api_login_is_allowed_before_authentication(self):
        request = SimpleNamespace(
            url=SimpleNamespace(path="/api/login"),
            session={},
            cookies={},
        )
        self.assertEqual(auth.verify_credentials(request, None), "bootstrap")

    def test_legacy_state_migrates_once_with_private_permissions(self):
        with (
            tempfile.TemporaryDirectory() as legacy_directory,
            tempfile.TemporaryDirectory() as state_directory,
        ):
            legacy = Path(legacy_directory)
            state = Path(state_directory)
            (legacy / "auth.json").write_text('{"hash": "legacy"}')
            (legacy / "operator.yaml").write_text("llm: {}\n")
            with (
                patch.dict("os.environ", {"LEGACY_STATE_DIR": str(legacy)}),
                patch.object(auth, "STATE_DIR", state),
                patch.object(auth, "AUTH_FILE", state / "auth.json"),
                patch.object(auth, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(auth, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(auth, "SESSION_SECRET_FILE", state / "session_secret"),
            ):
                self.assertTrue(auth.migrate_legacy_state())
                self.assertFalse(auth.migrate_legacy_state())
            self.assertEqual((state / "auth.json").read_text(), '{"hash": "legacy"}')
            self.assertEqual((state / "auth.json").stat().st_mode & 0o777, 0o600)

    def test_html_request_redirects_to_login_after_activation_without_basic_challenge(
        self,
    ):
        request = SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/settings", query=""),
            headers={"accept": "text/html"},
            session={},
            cookies={},
        )
        with patch.object(auth, "setup_complete", return_value=True):
            with self.assertRaises(auth.HTTPException) as raised:
                auth.verify_credentials(request, None)
        self.assertEqual(raised.exception.status_code, 303)
        self.assertEqual(raised.exception.headers["Location"], "/login?next=/settings")
        self.assertNotIn("WWW-Authenticate", raised.exception.headers)

    def test_json_request_returns_401_after_activation_without_basic_challenge(self):
        request = SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/api/system/health", query=""),
            headers={"accept": "application/json"},
            session={},
            cookies={},
        )
        with patch.object(auth, "setup_complete", return_value=True):
            with self.assertRaises(auth.HTTPException) as raised:
                auth.verify_credentials(request, None)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("WWW-Authenticate", raised.exception.headers or {})

    def test_activation_writes_marker_last_and_creates_session(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            request = SimpleNamespace(session={})
            with (
                patch.object(setup, "STATE_DIR", state),
                patch.object(setup, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(setup, "AUTH_FILE", state / "auth.json"),
                patch.object(setup, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(setup, "setup_complete", return_value=False),
                patch.object(setup, "_reload_or_restart", return_value=False),
                patch.object(
                    setup,
                    "_candidate_validator",
                    return_value=lambda _operator, _secrets: None,
                ),
            ):
                result = setup.activate(
                    setup.ActivationRequest(
                        password="a sufficiently long password",
                        profile={"llm": {"default_model": "test-model"}},
                        coverage={"fred": True},
                        secrets={"LLM_API_KEY": "private"},
                    ),
                    request,
                )
            self.assertTrue(result["activated"])
            self.assertEqual(result["version"], 1)
            self.assertEqual(result["restart_required"], False)
            self.assertTrue((state / "activated.json").is_symlink())
            self.assertTrue((state / "auth.json").is_symlink())
            self.assertTrue((state / "operator.yaml").is_symlink())
            self.assertTrue((state / "current").is_symlink())
            self.assertTrue((state / "versions" / "v1" / "auth.json").exists())
            self.assertEqual((state / "secrets.env").stat().st_mode & 0o777, 0o600)
            self.assertTrue(request.session["authenticated"])
            self.assertIn("issued_at", request.session)
            self.assertNotIn("csrf", request.session)
            marker = json.loads((state / "activated.json").read_text())
            self.assertEqual(marker["version"], 1)
            self.assertEqual(marker["layout"], "versions")

    def test_failed_activation_remains_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            request = SimpleNamespace(session={})
            with (
                patch.object(setup, "STATE_DIR", state),
                patch.object(setup, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(setup, "AUTH_FILE", state / "auth.json"),
                patch.object(setup, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(setup, "setup_complete", return_value=False),
                patch.object(
                    setup,
                    "commit_setup",
                    side_effect=OSError("storage failure"),
                ),
            ):
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.activate(
                        setup.ActivationRequest(
                            password="a sufficiently long password",
                            profile={},
                            secrets={},
                        ),
                        request,
                    )
            self.assertEqual(raised.exception.status_code, 500)
            self.assertFalse((state / "activated.json").exists())
            self.assertFalse((state / "auth.json").exists())
            self.assertFalse((state / "operator.yaml").exists())
            self.assertFalse((state / "versions").exists())

    def test_session_login_logout_and_html_redirect_flow(self):
        from fastapi.testclient import TestClient

        import main

        config = {
            "logging": {"level": "INFO"},
            "llm": {
                "base_url": "https://example.invalid/v1",
                "default_model": "test-model",
                "reasoning_effort": "high",
            },
            "budgets": {"daily_llm_usd": 1.0},
            "collectors": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            auth_file = state / "auth.json"
            operator_file = state / "operator.yaml"
            activation_file = state / "activated.json"
            auth_file.write_text(
                json.dumps(auth.hash_password("a sufficiently long password"))
            )
            operator_file.write_text("{}\n")
            activation_file.write_text('{"version": 1}\n')
            with (
                patch.object(auth, "STATE_DIR", state),
                patch.object(auth, "AUTH_FILE", auth_file),
                patch.object(auth, "OPERATOR_FILE", operator_file),
                patch.object(auth, "ACTIVATION_FILE", activation_file),
                patch.object(auth, "SECRETS_FILE", state / "secrets.env"),
                patch.object(setup, "STATE_DIR", state),
                patch.object(setup, "AUTH_FILE", auth_file),
                patch.object(setup, "ACTIVATION_FILE", activation_file),
                patch.object(main, "STATE_DIR", state),
                patch.object(main, "AUTH_FILE", auth_file),
                patch.object(main, "OPERATOR_FILE", operator_file),
                patch.object(main, "ACTIVATION_FILE", activation_file),
                patch.object(main, "load_config", return_value=config),
                patch(
                    "routes.views.settings.app_config.load_config",
                    return_value=config,
                ),
            ):
                client = TestClient(main.create_app())
                redirect = client.get(
                    "/settings",
                    headers={"accept": "text/html"},
                    follow_redirects=False,
                )
                self.assertEqual(redirect.status_code, 303)
                self.assertEqual(redirect.headers["location"], "/login?next=/settings")
                self.assertNotIn("www-authenticate", redirect.headers)

                login = client.post(
                    "/api/login",
                    json={"password": "a sufficiently long password"},
                )
                self.assertEqual(login.status_code, 200)
                csrf = login.json()["csrf_token"]
                self.assertEqual(client.get("/settings").status_code, 200)

                logout = client.post(
                    "/api/logout",
                    headers={
                        "x-csrf-token": csrf,
                        "Origin": "http://testserver",
                    },
                )
                self.assertEqual(logout.status_code, 200)
                self.assertEqual(
                    client.get(
                        "/settings",
                        headers={"accept": "text/html"},
                        follow_redirects=False,
                    ).status_code,
                    303,
                )

    def test_build_identity_is_public_and_does_not_expose_secrets(self):
        from fastapi.testclient import TestClient

        import main

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with (
                patch.object(main, "STATE_DIR", state),
                patch.object(main, "AUTH_FILE", state / "auth.json"),
                patch.object(main, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(main, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(
                    main, "load_config", return_value={"logging": {"level": "INFO"}}
                ),
            ):
                response = TestClient(main.create_app()).get("/api/meta/build")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertIn("commit", payload)
            self.assertEqual(payload["state"]["path"], str(state))
            self.assertNotIn("password", response.text.lower())
            self.assertNotIn("api_key", response.text.lower())


class DemoFreshVolumeBootstrapTests(unittest.TestCase):
    """A fresh demo volume must authenticate with the configured HTTP Basic
    credentials (demo/demo) and must never present the setup form. Production
    keeps the fail-closed setup bootstrap."""

    @staticmethod
    def _html_root_request():
        return SimpleNamespace(
            url=SimpleNamespace(path="/"),
            session={},
            cookies={},
            headers={"accept": "text/html"},
            method="GET",
        )

    def test_fresh_root_keeps_setup_bootstrap_in_production_with_legacy(self):
        request = self._html_root_request()
        with (
            patch.object(auth, "setup_complete", return_value=False),
            patch.dict(
                os.environ,
                {"DEPLOYMENT_MODE": "production", "LEGACY_BASIC_AUTH": "1"},
                clear=False,
            ),
        ):
            self.assertEqual(auth.verify_credentials(request, None), "bootstrap")

    def test_fresh_root_keeps_setup_bootstrap_without_configured_credentials(self):
        request = self._html_root_request()
        with (
            patch.object(auth, "setup_complete", return_value=False),
            patch.dict(
                os.environ,
                {
                    "DEPLOYMENT_MODE": "demo",
                    "LEGACY_BASIC_AUTH": "1",
                    "DASHBOARD_USER": "",
                    "DASHBOARD_PASSWORD": "",
                },
                clear=False,
            ),
        ):
            self.assertEqual(auth.verify_credentials(request, None), "bootstrap")

    def test_fresh_root_challenges_basic_in_demo_with_configured_credentials(self):
        request = self._html_root_request()
        with (
            patch.object(auth, "setup_complete", return_value=False),
            patch.dict(
                os.environ,
                {
                    "DEPLOYMENT_MODE": "demo",
                    "LEGACY_BASIC_AUTH": "1",
                    "DASHBOARD_USER": "demo",
                    "DASHBOARD_PASSWORD": "demo",
                },
                clear=False,
            ),
        ):
            with self.assertRaises(auth.HTTPException) as raised:
                auth.verify_credentials(request, None)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.headers, {"WWW-Authenticate": "Basic"})

    def test_demo_login_and_setup_pages_never_reach_the_setup_form(self):
        from fastapi import FastAPI
        from fastapi.templating import Jinja2Templates
        from fastapi.testclient import TestClient

        from routes.views import setup as setup_view

        app = FastAPI()
        app.state.templates = Jinja2Templates(
            directory=Path(__file__).resolve().parents[1] / "templates"
        )
        app.include_router(setup_view.router)
        with (
            patch.object(setup_view, "setup_complete", return_value=False),
            patch.dict(
                os.environ,
                {
                    "DEPLOYMENT_MODE": "test",
                    "LEGACY_BASIC_AUTH": "1",
                    "DASHBOARD_USER": "demo",
                    "DASHBOARD_PASSWORD": "demo",
                },
                clear=False,
            ),
        ):
            client = TestClient(app)
            login = client.get("/login", follow_redirects=False)
            self.assertEqual(login.status_code, 303)
            self.assertEqual(login.headers["location"], "/")
            setup = client.get("/setup", follow_redirects=False)
            self.assertEqual(setup.status_code, 303)
            self.assertEqual(setup.headers["location"], "/")

    def test_production_login_and_setup_pages_keep_the_setup_bootstrap(self):
        from fastapi import FastAPI
        from fastapi.templating import Jinja2Templates
        from fastapi.testclient import TestClient

        from routes.views import setup as setup_view

        app = FastAPI()
        app.state.templates = Jinja2Templates(
            directory=Path(__file__).resolve().parents[1] / "templates"
        )
        app.include_router(setup_view.router)
        with (
            patch.object(setup_view, "setup_complete", return_value=False),
            patch.dict(
                os.environ,
                {"DEPLOYMENT_MODE": "production", "LEGACY_BASIC_AUTH": "1"},
                clear=False,
            ),
        ):
            client = TestClient(app)
            login = client.get("/login", follow_redirects=False)
            self.assertEqual(login.status_code, 303)
            self.assertEqual(login.headers["location"], "/setup")
            setup = client.get("/setup", follow_redirects=False)
            self.assertEqual(setup.status_code, 200)
            self.assertIn('id="setup-token"', setup.text)

    def test_fresh_demo_root_end_to_end_authenticates_with_basic_credentials(self):
        import base64

        from fastapi.testclient import TestClient

        import main

        config = {
            "logging": {"level": "INFO"},
            "llm": {
                "base_url": "https://example.invalid/v1",
                "default_model": "test-model",
                "reasoning_effort": "high",
            },
            "budgets": {"daily_llm_usd": 1.0},
            "collectors": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with (
                patch.object(auth, "STATE_DIR", state),
                patch.object(auth, "AUTH_FILE", state / "auth.json"),
                patch.object(auth, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(auth, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(auth, "SECRETS_FILE", state / "secrets.env"),
                patch.object(setup, "STATE_DIR", state),
                patch.object(setup, "AUTH_FILE", state / "auth.json"),
                patch.object(setup, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(main, "STATE_DIR", state),
                patch.object(main, "AUTH_FILE", state / "auth.json"),
                patch.object(main, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(main, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(main, "load_config", return_value=config),
                patch(
                    "routes.views.settings.app_config.load_config",
                    return_value=config,
                ),
            ):
                client = TestClient(main.create_app())
                root = client.get(
                    "/", headers={"accept": "text/html"}, follow_redirects=False
                )
                self.assertEqual(root.status_code, 401)
                self.assertEqual(root.headers.get("www-authenticate"), "Basic")
                self.assertNotEqual(root.headers.get("location"), "/setup")
                login = client.get(
                    "/login", headers={"accept": "text/html"}, follow_redirects=False
                )
                self.assertEqual(login.status_code, 303)
                self.assertEqual(login.headers["location"], "/")
                setup_page = client.get(
                    "/setup", headers={"accept": "text/html"}, follow_redirects=False
                )
                self.assertEqual(setup_page.status_code, 303)
                self.assertEqual(setup_page.headers["location"], "/")
                credentials = base64.b64encode(b"test:test").decode()
                dashboard = client.get(
                    "/",
                    headers={
                        "accept": "text/html",
                        "Authorization": f"Basic {credentials}",
                    },
                    follow_redirects=False,
                )
                self.assertEqual(dashboard.status_code, 200)


class AuthSigningSecurityTests(unittest.TestCase):
    """Fail-closed signing keys, CSRF enforcement, and middleware ordering."""

    PASSWORD = "a sufficiently long password"
    ORIGIN = "https://testserver"

    @staticmethod
    def _write_state(state: Path):
        auth_file = state / "auth.json"
        operator_file = state / "operator.yaml"
        activation_file = state / "activated.json"
        auth_file.write_text(
            json.dumps(auth.hash_password(AuthSigningSecurityTests.PASSWORD))
        )
        operator_file.write_text("{}\n")
        activation_file.write_text('{"version": 1}\n')
        return auth_file, operator_file, activation_file

    @contextmanager
    def _app(self, state: Path, config: dict | None = None):
        import main
        from routes.json import setup as setup_routes

        config = config or {"logging": {"level": "INFO"}}
        auth_file, operator_file, activation_file = self._write_state(state)
        with ExitStack() as stack:
            for patcher in (
                patch.object(auth, "STATE_DIR", state),
                patch.object(auth, "AUTH_FILE", auth_file),
                patch.object(auth, "OPERATOR_FILE", operator_file),
                patch.object(auth, "ACTIVATION_FILE", activation_file),
                patch.object(setup_routes, "STATE_DIR", state),
                patch.object(setup_routes, "AUTH_FILE", auth_file),
                patch.object(setup_routes, "ACTIVATION_FILE", activation_file),
                patch.object(
                    setup_routes,
                    "_candidate_validator",
                    return_value=lambda _operator, _secrets: None,
                ),
                patch.object(main, "STATE_DIR", state),
                patch.object(main, "AUTH_FILE", auth_file),
                patch.object(main, "OPERATOR_FILE", operator_file),
                patch.object(main, "ACTIVATION_FILE", activation_file),
                patch.object(main, "load_config", return_value=config),
                patch(
                    "routes.views.settings.app_config.load_config",
                    return_value=config,
                ),
                patch(
                    "routes.json.settings.load_config",
                    return_value=config,
                ),
            ):
                stack.enter_context(patcher)
            with auth._login_attempts_lock:
                auth._login_attempts.clear()
            yield main.create_app()

    def _login(self, client):
        response = client.post("/api/login", json={"password": self.PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_unauthenticated_protected_requests_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._app(Path(directory)) as app:
                from fastapi.testclient import TestClient

                client = TestClient(app, base_url=self.ORIGIN)
                api = client.get("/api/settings/timezone")
                self.assertEqual(api.status_code, 401)
                page = client.get(
                    "/settings",
                    headers={"accept": "text/html"},
                    follow_redirects=False,
                )
                self.assertEqual(page.status_code, 303)
                self.assertEqual(page.headers["location"], "/login?next=/settings")

    def test_public_request_bodies_are_limited_before_route_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._app(Path(directory)) as app:
                from fastapi.testclient import TestClient

                client = TestClient(app, base_url=self.ORIGIN)
                with patch.object(setup, "verify_login_password") as verify:
                    for content_type in (
                        "application/json",
                        "text/plain",
                        "application/octet-stream",
                    ):
                        with self.subTest(content_type=content_type):
                            declared = client.post(
                                "/api/login",
                                content=b"{}",
                                headers={
                                    "Content-Type": content_type,
                                    "Content-Length": str(4 * 1024 + 1),
                                },
                            )
                            self.assertEqual(declared.status_code, 413)

                            def oversized_chunks():
                                yield b'{"password":"'
                                yield b"x" * (4 * 1024)
                                yield b'"}'

                            chunked = client.post(
                                "/api/login",
                                content=oversized_chunks(),
                                headers={"Content-Type": content_type},
                            )
                            self.assertEqual(chunked.status_code, 413)
                    verify.assert_not_called()

    def test_login_hash_budget_exhaustion_returns_retryable_429(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._app(Path(directory)) as app:
                from fastapi.testclient import TestClient

                client = TestClient(app, base_url=self.ORIGIN)
                with patch.object(
                    setup,
                    "verify_login_password",
                    side_effect=auth.LoginRateLimited(17),
                ):
                    response = client.post(
                        "/api/login", json={"password": self.PASSWORD}
                    )
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers["retry-after"], "17")
                self.assertEqual(response.json()["detail"], "Too many login attempts")

    def test_authenticated_post_requires_valid_csrf_token(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._app(Path(directory)) as app:
                from fastapi.testclient import TestClient

                client = TestClient(app, base_url=self.ORIGIN)
                login = self._login(client)
                self.assertTrue(
                    auth.verify_csrf_token(login.json()["csrf_token"]),
                    "login must return a verified signed CSRF token",
                )
                # Session-authenticated mutations are never machine-exempt:
                # a JSON POST with no Origin is rejected, not passed through.
                machine = client.post(
                    "/api/settings/timezone", json={"timezone": "UTC"}
                )
                self.assertEqual(machine.status_code, 403)
                # Authenticated GET mints the CSRF cookie.
                self.assertEqual(client.get("/settings").status_code, 200)
                token = client.cookies.get(auth.CSRF_COOKIE)
                self.assertTrue(auth.verify_csrf_token(token))
                # Browser-style mutation without a token is rejected.
                no_token = client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers={"Origin": self.ORIGIN},
                )
                self.assertEqual(no_token.status_code, 403)
                # Invalid token is rejected.
                invalid = client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers={"Origin": self.ORIGIN, "X-CSRF-Token": "not-a-token"},
                )
                self.assertEqual(invalid.status_code, 403)
                # Valid token is accepted.
                valid = client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers={"Origin": self.ORIGIN, "X-CSRF-Token": token},
                )
                self.assertEqual(valid.status_code, 200, valid.text)

    def test_machine_json_exemption_requires_authorization_and_no_session(self):
        import base64

        import main
        from routes.json import setup as setup_routes

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with (
                patch.object(auth, "STATE_DIR", state),
                patch.object(auth, "AUTH_FILE", state / "auth.json"),
                patch.object(auth, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(auth, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(setup_routes, "STATE_DIR", state),
                patch.object(setup_routes, "AUTH_FILE", state / "auth.json"),
                patch.object(setup_routes, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(main, "STATE_DIR", state),
                patch.object(main, "AUTH_FILE", state / "auth.json"),
                patch.object(main, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(main, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(
                    main, "load_config", return_value={"logging": {"level": "INFO"}}
                ),
            ):
                from fastapi.testclient import TestClient

                client = TestClient(main.create_app(), base_url=self.ORIGIN)
                auth_headers = {
                    "Authorization": "Basic " + base64.b64encode(b"test:test").decode()
                }
                # Pre-activation basic-auth API call: JSON, no browser signals.
                machine = client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers=auth_headers,
                )
                self.assertEqual(machine.status_code, 200, machine.text)
                # The same client with a browser signal loses the exemption.
                browser = client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers={**auth_headers, "Origin": self.ORIGIN},
                )
                self.assertEqual(browser.status_code, 403)

    def test_mismatched_valid_csrf_tokens_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._app(Path(directory)) as app:
                from fastapi.testclient import TestClient

                client = TestClient(app, base_url=self.ORIGIN)
                self._login(client)
                self.assertEqual(client.get("/settings").status_code, 200)
                cookie_token = client.cookies.get(auth.CSRF_COOKIE)
                other_token = auth.mint_csrf_token()
                self.assertNotEqual(cookie_token, other_token)
                # A validly signed but different token is refused: the header
                # must match the cookie exactly (double submit).
                mismatched = client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers={"Origin": self.ORIGIN, "X-CSRF-Token": other_token},
                )
                self.assertEqual(mismatched.status_code, 403)
                ok = client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers={"Origin": self.ORIGIN, "X-CSRF-Token": cookie_token},
                )
                self.assertEqual(ok.status_code, 200, ok.text)

    def test_login_token_matches_response_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._app(Path(directory)) as app:
                from fastapi.testclient import TestClient

                client = TestClient(app, base_url=self.ORIGIN)
                login = self._login(client)
                body_token = login.json()["csrf_token"]
                cookie_header = next(
                    value
                    for value in login.headers.get_list("set-cookie")
                    if value.startswith(auth.CSRF_COOKIE + "=")
                )
                cookie_token = cookie_header.split(";", 1)[0].split("=", 1)[1]
                self.assertEqual(body_token, cookie_token)
                # The next mutation works immediately with the body token and
                # the cookie set on the login response.
                response = client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers={"Origin": self.ORIGIN, "X-CSRF-Token": body_token},
                )
                self.assertEqual(response.status_code, 200, response.text)

    def test_machine_client_survives_sequential_mutations(self):
        import base64

        import main
        from routes.json import setup as setup_routes

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with (
                patch.object(auth, "STATE_DIR", state),
                patch.object(auth, "AUTH_FILE", state / "auth.json"),
                patch.object(auth, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(auth, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(setup_routes, "STATE_DIR", state),
                patch.object(setup_routes, "AUTH_FILE", state / "auth.json"),
                patch.object(setup_routes, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(main, "STATE_DIR", state),
                patch.object(main, "AUTH_FILE", state / "auth.json"),
                patch.object(main, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(main, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(
                    main, "load_config", return_value={"logging": {"level": "INFO"}}
                ),
            ):
                from fastapi.testclient import TestClient

                client = TestClient(main.create_app(), base_url=self.ORIGIN)
                auth_headers = {
                    "Authorization": "Basic " + base64.b64encode(b"test:test").decode()
                }
                first = client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers=auth_headers,
                )
                self.assertEqual(first.status_code, 200, first.text)
                # The stateless machine call must not have received a CSRF
                # cookie that would poison the next request's machine exemption.
                self.assertIsNone(client.cookies.get(auth.CSRF_COOKIE))
                second = client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers=auth_headers,
                )
                self.assertEqual(second.status_code, 200, second.text)

    def test_setup_subpaths_require_csrf_after_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._app(Path(directory)) as app:
                from fastapi.testclient import TestClient

                client = TestClient(app, base_url=self.ORIGIN)
                self._login(client)
                self.assertEqual(client.get("/settings").status_code, 200)
                token = client.cookies.get(auth.CSRF_COOKIE)
                headers = {"Origin": self.ORIGIN, "X-CSRF-Token": token}
                with patch("routes.json.setup._reload_or_restart", return_value=False):
                    rejected = client.put(
                        "/api/setup/profile",
                        json={"profile": {"llm": {"default_model": "x"}}},
                        headers={"Origin": self.ORIGIN},
                    )
                    self.assertEqual(rejected.status_code, 403)
                    accepted = client.put(
                        "/api/setup/profile",
                        json={"profile": {"llm": {"default_model": "x"}}},
                        headers=headers,
                    )
                    self.assertEqual(accepted.status_code, 200, accepted.text)
                    with patch(
                        "httpx.get",
                        side_effect=httpx.ConnectError("unreachable"),
                    ):
                        rejected = client.post(
                            "/api/setup/test-connection",
                            json={"base_url": "https://example.invalid/v1"},
                            headers={"Origin": self.ORIGIN},
                        )
                        self.assertEqual(rejected.status_code, 403)
                        reached = client.post(
                            "/api/setup/test-connection",
                            json={"provider": "openrouter"},
                            headers=headers,
                        )
                        self.assertEqual(reached.status_code, 422, reached.text)

    def test_canonical_external_origin_governs_csrf(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {"EXTERNAL_ORIGIN": "https://dash.example.com"},
                clear=False,
            ):
                with self._app(Path(directory)) as app:
                    from fastapi.testclient import TestClient

                    client = TestClient(app, base_url=self.ORIGIN)
                    self._login(client)
                    self.assertEqual(client.get("/settings").status_code, 200)
                    token = client.cookies.get(auth.CSRF_COOKIE)
                    # The request base URL matches, but the canonical origin rules.
                    wrong = client.post(
                        "/api/settings/timezone",
                        json={"timezone": "UTC"},
                        headers={
                            "Origin": self.ORIGIN,
                            "X-CSRF-Token": token,
                        },
                    )
                    self.assertEqual(wrong.status_code, 403)
                    right = client.post(
                        "/api/settings/timezone",
                        json={"timezone": "UTC"},
                        headers={
                            "Origin": "https://dash.example.com",
                            "X-CSRF-Token": token,
                        },
                    )
                    self.assertEqual(right.status_code, 200, right.text)

    def test_disable_auth_is_rejected_in_production_and_honored_in_test(self):
        with patch.dict(
            os.environ,
            {
                "DEPLOYMENT_MODE": "production",
                "DISABLE_AUTH": "1",
                "SESSION_SIGNING_KEY": "sK-9xQ5wN1pL2cR-7zM8vB4tS2aA1-dD4eE5fF6gG7hH8",
                "CSRF_SIGNING_KEY": "cR-7pL2xQ9sK-4zM8vB6tS2wW1-aA3dD5eE7fF9gG2hH4",
                "SSE_SIGNING_KEY": "eT-3zM8vB4xQ7wN1sK-9pL2cR-5tS8aA2-dD6eE1fF4gG7hH3",
                "EXTERNAL_ORIGIN": "https://dash.example.com",
            },
            clear=False,
        ):
            import main

            with self.assertRaisesRegex(RuntimeError, "DISABLE_AUTH"):
                main.create_app()
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"DISABLE_AUTH": "1"}, clear=False):
                with self._app(Path(directory)) as app:
                    from fastapi.testclient import TestClient

                    client = TestClient(app, base_url=self.ORIGIN)
                    self.assertEqual(
                        client.get("/api/settings/timezone").status_code, 200
                    )

    def test_production_rejects_absent_placeholder_low_entropy_and_reused_keys(
        self,
    ):
        import main

        base = {
            "DEPLOYMENT_MODE": "production",
            "SESSION_SIGNING_KEY": "sK-9xQ5wN1pL2cR-7zM8vB4tS2aA1-dD4eE5fF6gG7hH8",
            "CSRF_SIGNING_KEY": "cR-7pL2xQ9sK-4zM8vB6tS2wW1-aA3dD5eE7fF9gG2hH4",
            "SSE_SIGNING_KEY": "eT-3zM8vB4xQ7wN1sK-9pL2cR-5tS8aA2-dD6eE1fF4gG7hH3",
            "EXTERNAL_ORIGIN": "https://dash.example.com",
        }
        cases = (
            (
                "absent session key",
                {"SESSION_SIGNING_KEY": ""},
                "SESSION_SIGNING_KEY is not set",
            ),
            (
                "absent csrf key",
                {"CSRF_SIGNING_KEY": ""},
                "CSRF_SIGNING_KEY is not set",
            ),
            (
                "absent sse key",
                {"SSE_SIGNING_KEY": ""},
                "SSE_SIGNING_KEY is not set",
            ),
            ("placeholder", {"CSRF_SIGNING_KEY": "changeme"}, "placeholder"),
            ("low-entropy", {"SSE_SIGNING_KEY": "short"}, "low-entropy"),
            (
                "reused keys",
                {"CSRF_SIGNING_KEY": base["SESSION_SIGNING_KEY"]},
                "must be distinct",
            ),
            (
                "absent origin",
                {"EXTERNAL_ORIGIN": ""},
                "EXTERNAL_ORIGIN is required",
            ),
        )
        for label, overrides, message in cases:
            with self.subTest(label=label):
                env = dict(base)
                env.update(overrides)
                with patch.dict(os.environ, env, clear=False):
                    with self.assertRaisesRegex(RuntimeError, message):
                        main.create_app()

    def test_canonical_origin_normalization_and_validation(self):
        with patch.dict(
            os.environ,
            {"DEPLOYMENT_MODE": "test", "EXTERNAL_ORIGIN": "https://dash.example.com/"},
            clear=False,
        ):
            self.assertEqual(auth.canonical_origin(), ("https", "dash.example.com"))
        with patch.dict(
            os.environ,
            {
                "DEPLOYMENT_MODE": "test",
                "EXTERNAL_ORIGIN": "https://dash.example.com:443",
            },
            clear=False,
        ):
            self.assertEqual(auth.canonical_origin(), ("https", "dash.example.com"))
        with patch.dict(
            os.environ,
            {
                "DEPLOYMENT_MODE": "test",
                "EXTERNAL_ORIGIN": "http://dash.example.com:80",
            },
            clear=False,
        ):
            self.assertEqual(auth.canonical_origin(), ("http", "dash.example.com"))
        with patch.dict(
            os.environ,
            {
                "DEPLOYMENT_MODE": "test",
                "EXTERNAL_ORIGIN": "https://dash.example.com:8443",
            },
            clear=False,
        ):
            self.assertEqual(
                auth.canonical_origin(), ("https", "dash.example.com:8443")
            )
        cases = (
            ("https://user:pass@dash.example.com", "userinfo"),
            ("https://dash.example.com/path", "valid origin"),
            ("https://dash.example.com?x=1", "valid origin"),
            ("https://dash.example.com:abc", "valid origin"),
            ("not-an-origin", "full origin"),
        )
        for origin, message in cases:
            with self.subTest(origin=origin):
                with patch.dict(
                    os.environ,
                    {"DEPLOYMENT_MODE": "test", "EXTERNAL_ORIGIN": origin},
                    clear=False,
                ):
                    with self.assertRaisesRegex(RuntimeError, message):
                        auth.canonical_origin()

    def test_trusted_hosts_required_in_production_and_enforced(self):
        import main

        keys = {
            "DEPLOYMENT_MODE": "production",
            "SESSION_SIGNING_KEY": "sK-9xQ5wN1pL2cR-7zM8vB4tS2aA1-dD4eE5fF6gG7hH8",
            "CSRF_SIGNING_KEY": "cR-7pL2xQ9sK-4zM8vB6tS2wW1-aA3dD5eE7fF9gG2hH4",
            "SSE_SIGNING_KEY": "eT-3zM8vB4xQ7wN1sK-9pL2cR-5tS8aA2-dD6eE1fF4gG7hH3",
            "EXTERNAL_ORIGIN": "https://dash.example.com",
            "DASHBOARD_USER": "admin",
            "DASHBOARD_PASSWORD": "a sufficiently long password",
            "COOKIE_SECURE": "1",
        }
        with patch.dict(os.environ, keys, clear=False):
            with self.assertRaisesRegex(RuntimeError, "TRUSTED_HOSTS"):
                main.create_app()
        invalid_entries = (
            "not a host",
            "https://dash.example.com",
            "*",
            "*.",
            "*foo",
            "foo*",
            "*.example.*.com",
            "[::1]",
            "::1",
            "example.com:8080",
        )
        for entry in invalid_entries:
            with self.subTest(entry=entry):
                with patch.dict(
                    os.environ,
                    {**keys, "TRUSTED_HOSTS": entry},
                    clear=False,
                ):
                    with self.assertRaisesRegex(RuntimeError, "invalid host"):
                        main.create_app()
        # Safe subdomain wildcards and IPv4 are accepted in production.
        with patch.dict(
            os.environ,
            {**keys, "TRUSTED_HOSTS": "localhost,127.0.0.1,*.example.com"},
            clear=False,
        ):
            with patch(
                "config.load_config", return_value={"logging": {"level": "INFO"}}
            ):
                main.create_app()
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ, {"TRUSTED_HOSTS": "localhost,127.0.0.1"}, clear=False
            ):
                with self._app(Path(directory)) as app:
                    from fastapi.testclient import TestClient

                    allowed = TestClient(app, base_url="http://localhost")
                    self.assertEqual(
                        allowed.get("/api/settings/timezone").status_code, 401
                    )
                    denied = TestClient(app, base_url="http://testserver")
                    self.assertEqual(
                        denied.get("/api/settings/timezone").status_code, 400
                    )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ, {"TRUSTED_HOSTS": "*.example.com"}, clear=False
            ):
                with self._app(Path(directory)) as app:
                    from fastapi.testclient import TestClient

                    subdomain = TestClient(app, base_url="http://api.example.com")
                    self.assertEqual(
                        subdomain.get("/api/settings/timezone").status_code, 401
                    )
                    outside = TestClient(app, base_url="http://testserver")
                    self.assertEqual(
                        outside.get("/api/settings/timezone").status_code, 400
                    )

    def test_session_max_age_is_validated_at_startup(self):
        import main

        with patch("config.load_config", return_value={"logging": {"level": "INFO"}}):
            with patch.dict(
                os.environ, {"SESSION_MAX_AGE_SECONDS": "abc"}, clear=False
            ):
                with self.assertRaisesRegex(RuntimeError, "SESSION_MAX_AGE_SECONDS"):
                    main.create_app()
            for bad in ("-1", "0", "99999999999999999999"):
                with self.subTest(value=bad):
                    with patch.dict(
                        os.environ, {"SESSION_MAX_AGE_SECONDS": bad}, clear=False
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError, "SESSION_MAX_AGE_SECONDS"
                        ):
                            main.create_app()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(auth.session_max_age_seconds(), 43200)
        with patch.dict(os.environ, {"SESSION_MAX_AGE_SECONDS": "3600"}, clear=False):
            self.assertEqual(auth.session_max_age_seconds(), 3600)

    def test_cookie_secure_must_match_external_origin_scheme(self):
        import main

        base = {
            "DEPLOYMENT_MODE": "production",
            "SESSION_SIGNING_KEY": "sK-9xQ5wN1pL2cR-7zM8vB4tS2aA1-dD4eE5fF6gG7hH8",
            "CSRF_SIGNING_KEY": "cR-7pL2xQ9sK-4zM8vB6tS2wW1-aA3dD5eE7fF9gG2hH4",
            "SSE_SIGNING_KEY": "eT-3zM8vB4xQ7wN1sK-9pL2cR-5tS8aA2-dD6eE1fF4gG7hH3",
            "TRUSTED_HOSTS": "localhost,127.0.0.1",
            "DASHBOARD_USER": "admin",
            "DASHBOARD_PASSWORD": "a sufficiently long password",
        }
        with patch("config.load_config", return_value={"logging": {"level": "INFO"}}):
            # HTTPS origin with Secure disabled or malformed is refused.
            for cookie_secure_value in ("0", "false", "banana"):
                with self.subTest(cookie_secure=cookie_secure_value):
                    env = {
                        **base,
                        "EXTERNAL_ORIGIN": "https://dash.example.com",
                        "COOKIE_SECURE": cookie_secure_value,
                    }
                    with patch.dict(os.environ, env, clear=False):
                        with self.assertRaisesRegex(RuntimeError, "COOKIE_SECURE"):
                            main.create_app()
            # Remote production HTTP is refused regardless of cookie setting.
            for cookie_secure_value in ("0", "1"):
                with self.subTest(remote_http_cookie=cookie_secure_value):
                    with patch.dict(
                        os.environ,
                        {
                            **base,
                            "EXTERNAL_ORIGIN": "http://dash.example.com",
                            "COOKIE_SECURE": cookie_secure_value,
                        },
                        clear=False,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "must use HTTPS"):
                            main.create_app()
            # Local HTTP origin with Secure disabled stays allowed.
            with patch.dict(
                os.environ,
                {
                    **base,
                    "EXTERNAL_ORIGIN": "http://127.0.0.1:8000",
                    "COOKIE_SECURE": "0",
                },
                clear=False,
            ):
                main.create_app()
            # HTTPS with Secure enabled passes.
            with patch.dict(
                os.environ,
                {
                    **base,
                    "EXTERNAL_ORIGIN": "https://dash.example.com",
                    "COOKIE_SECURE": "1",
                },
                clear=False,
            ):
                main.create_app()
        # demo/test tolerate any combination.
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {"EXTERNAL_ORIGIN": "https://dash.example.com", "COOKIE_SECURE": "0"},
                clear=False,
            ):
                with self._app(Path(directory)) as app:
                    self.assertIsNotNone(app)

    def test_cookie_secure_attribute_follows_cookie_secure(self):
        for value, expected in (("1", True), ("0", False)):
            with self.subTest(cookie_secure=value):
                with tempfile.TemporaryDirectory() as directory:
                    with patch.dict(os.environ, {"COOKIE_SECURE": value}, clear=False):
                        with self._app(Path(directory)) as app:
                            from fastapi.testclient import TestClient

                            client = TestClient(app, base_url=self.ORIGIN)
                            login = self._login(client)
                            session_cookie = next(
                                cookie
                                for cookie in login.headers.get_list("set-cookie")
                                if cookie.startswith("market_session=")
                            )
                            self.assertEqual("Secure" in session_cookie, expected)
                            page = client.get("/settings")
                            self.assertEqual(page.status_code, 200)
                            csrf_cookie = next(
                                cookie
                                for cookie in page.headers.get_list("set-cookie")
                                if cookie.startswith("csrf-token=")
                            )
                            self.assertEqual("Secure" in csrf_cookie, expected)

    def test_production_rejects_placeholder_internal_credentials(self):
        import main

        base = {
            "DEPLOYMENT_MODE": "production",
            "SESSION_SIGNING_KEY": "sK-9xQ5wN1pL2cR-7zM8vB4tS2aA1-dD4eE5fF6gG7hH8",
            "CSRF_SIGNING_KEY": "cR-7pL2xQ9sK-4zM8vB6tS2wW1-aA3dD5eE7fF9gG2hH4",
            "SSE_SIGNING_KEY": "eT-3zM8vB4xQ7wN1sK-9pL2cR-5tS8aA2-dD6eE1fF4gG7hH3",
            "EXTERNAL_ORIGIN": "https://dash.example.com",
            "TRUSTED_HOSTS": "localhost,127.0.0.1",
            "DASHBOARD_USER": "admin",
            "DASHBOARD_PASSWORD": "a sufficiently long password",
            "COOKIE_SECURE": "1",
        }
        cases = (
            (
                "placeholder password",
                {"DASHBOARD_PASSWORD": "replace-with-a-strong-operator-password"},
                "DASHBOARD_PASSWORD must not be a placeholder value",
            ),
            (
                "default password",
                {"DASHBOARD_PASSWORD": "changeme"},
                "DASHBOARD_PASSWORD must not be a placeholder value",
            ),
            (
                "demo password",
                {"DASHBOARD_PASSWORD": "demo"},
                "DASHBOARD_PASSWORD must not be a placeholder value",
            ),
            (
                "short password",
                {"DASHBOARD_PASSWORD": "short"},
                "DASHBOARD_PASSWORD must be at least 12 characters",
            ),
            (
                "empty password",
                {"DASHBOARD_PASSWORD": ""},
                "DASHBOARD_PASSWORD must be set in production mode",
            ),
            (
                "placeholder user",
                {"DASHBOARD_USER": "replace-with-a-user"},
                "DASHBOARD_USER must not be a placeholder value",
            ),
            (
                "empty user",
                {"DASHBOARD_USER": ""},
                "DASHBOARD_USER must be set in production mode",
            ),
        )
        for label, overrides, message in cases:
            with self.subTest(label=label):
                env = dict(base)
                env.update(overrides)
                with patch.dict(os.environ, env, clear=False):
                    with self.assertRaisesRegex(RuntimeError, message):
                        main.create_app()
        # Valid credentials pass startup validation (config load patched).
        with patch.dict(os.environ, base, clear=False):
            with patch(
                "config.load_config", return_value={"logging": {"level": "INFO"}}
            ):
                main.create_app()

    def test_signing_keys_are_purpose_separated(self):
        key_a = "aA1-bB2-cC3-dD4-eE5-fF6-gG7-hH8-" * 2
        key_b = "zZ9-yY8-xX7-wW6-vV5-uU4-tT3-sS2-" * 2
        with patch.dict(
            os.environ,
            {
                "DEPLOYMENT_MODE": "test",
                "CSRF_SIGNING_KEY": key_a,
                "SSE_SIGNING_KEY": key_b,
            },
            clear=False,
        ):
            csrf_token = auth.mint_csrf_token()
            sse_token = auth.mint_sse_token()
            self.assertTrue(auth.verify_csrf_token(csrf_token))
            self.assertTrue(auth.verify_sse_token(sse_token, "/api/quotes/stream"))
            # Tokens never cross purposes or keys.
            self.assertFalse(auth.verify_csrf_token(sse_token))
            self.assertFalse(auth.verify_sse_token(csrf_token, "/api/quotes/stream"))
            self.assertFalse(
                auth.verify_csrf_token(auth.mint_sse_token()),
                "SSE token must not verify as CSRF even with its own key",
            )
        # A token minted under one key must not verify under another.
        with patch.dict(
            os.environ,
            {"DEPLOYMENT_MODE": "test", "CSRF_SIGNING_KEY": key_a},
            clear=False,
        ):
            minted = auth.mint_csrf_token()
        with patch.dict(
            os.environ,
            {"DEPLOYMENT_MODE": "test", "CSRF_SIGNING_KEY": key_b},
            clear=False,
        ):
            self.assertFalse(auth.verify_csrf_token(minted))

    def test_session_key_rotation_verifies_previous_sessions(self):
        current = "sK-9xQ5wN1pL2cR-7zM8vB4tS2aA1-dD4eE5fF6gG7hH8"
        previous = "cR-7pL2xQ9sK-4zM8vB6tS2wW1-aA3dD5eE7fF9gG2hH4"
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {
                    "DEPLOYMENT_MODE": "test",
                    "SESSION_SIGNING_KEY": current,
                    "SESSION_SIGNING_KEY_PREVIOUS": previous,
                },
                clear=False,
            ):
                with self._app(Path(directory)) as app:
                    from fastapi.testclient import TestClient

                    client = TestClient(app, base_url=self.ORIGIN)
                    old_cookie = auth.sign_session_cookie(
                        {
                            "authenticated": True,
                            "issued_at": int(time.time()),
                        },
                        secret=previous.encode(),
                    )
                    client.cookies.set("market_session", old_cookie)
                    self.assertEqual(client.get("/settings").status_code, 200)
                    # A session forged under the current key without the previous
                    # key fails to verify.
                    forged = auth.sign_session_cookie(
                        {"authenticated": True, "issued_at": int(time.time())},
                        secret=current.encode(),
                    )
                    client.cookies.clear()
                    forged = forged[:-1] + ("A" if forged[-1] != "A" else "B")
                    client.cookies.set("market_session", forged)
                    self.assertEqual(
                        client.get(
                            "/settings",
                            headers={"accept": "text/html"},
                            follow_redirects=False,
                        ).status_code,
                        303,
                    )
                    # New sessions are issued under the current key.
                    client.cookies.clear()
                    self._login(client)
                    issued = client.cookies.get("market_session")
                    self.assertTrue(
                        auth.decode_session_cookie(issued, secret=current.encode())
                    )
                    self.assertFalse(
                        auth.decode_session_cookie(issued, secret=previous.encode())
                    )

    def test_signing_key_entropy_floor_rejects_weak_patterns(self):
        import base64 as b64

        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "production"}, clear=False):
            weak = (
                "abcd" * 11,  # 44 chars, 4 distinct - tiny search space
                "a" * 64,  # single character
                "AbCdEfGh" * 5,  # 40 chars, 8 distinct, decodes under 32 bytes
                b64.b64encode(b"0" * 16).decode(),  # only 16 bytes encoded
                "0" * 64,  # hex alphabet, 1 distinct
                "a sufficiently long password",  # prose with spaces, not a secret
            )
            for key in weak:
                with self.subTest(key=key[:24]):
                    self.assertIsNotNone(auth._signing_key_problem(key), key)
            strong = (
                b64.urlsafe_b64encode(__import__("secrets").token_bytes(48))
                .decode()
                .rstrip("="),  # 64 chars, 48 decoded bytes
                "0123456789abcdef" * 4,  # 64 hex chars, 32 decoded bytes
                "sK-9xQ5wN1pL2cR-7zM8vB4tS2aA1-dD4eE5fF6gG7hH8",
            )
            for key in strong:
                with self.subTest(key=key[:24]):
                    self.assertIsNone(auth._signing_key_problem(key), key)

    def test_login_returns_verified_signed_token(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._app(Path(directory)) as app:
                from fastapi.testclient import TestClient

                client = TestClient(app, base_url=self.ORIGIN)
                login = self._login(client)
                token = login.json()["csrf_token"]
                self.assertTrue(auth.verify_csrf_token(token))
                self.assertNotIn("csrf", login.json())


if __name__ == "__main__":
    unittest.main()
