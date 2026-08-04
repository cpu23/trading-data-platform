import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# unittest discovery imports test modules as top-level modules, so package-level
# test fixtures are not guaranteed to run before auth captures its state paths.
_TEST_STATE_DIR = tempfile.mkdtemp(prefix="trading-api-auth-tests-")
os.environ["STATE_DIR"] = _TEST_STATE_DIR
os.environ["LEGACY_STATE_DIR"] = ""
os.environ["LEGACY_BASIC_AUTH"] = "true"
os.environ["DASHBOARD_USER"] = "test"
os.environ["DASHBOARD_PASSWORD"] = "test"

from types import SimpleNamespace

import auth
from routes.json import setup


class AuthSecurityTests(unittest.TestCase):
    def test_password_hash_round_trip(self):
        record = auth.hash_password("a sufficiently long password")
        self.assertTrue(auth.verify_password("a sufficiently long password", record))
        self.assertFalse(auth.verify_password("wrong password", record))

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
                setup.activate({"password": "too-short"}, object())
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
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with patch.object(setup, "STATE_DIR", state):
                setup._write_secrets({"LLM_API_KEY": "existing"})
                setup._write_secrets({"LLM_API_KEY": ""})
                self.assertEqual(setup._read_secrets()["LLM_API_KEY"], "existing")

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
                patch.object(setup, "setup_complete", return_value=False),
                patch.object(setup, "reload_config", return_value={}),
            ):
                result = setup.activate(
                    {
                        "password": "a sufficiently long password",
                        "profile": {"llm": {"default_model": "test-model"}},
                        "coverage": {"fred": True},
                        "secrets": {"LLM_API_KEY": "private"},
                    },
                    request,
                )
            self.assertTrue(result["activated"])
            self.assertTrue((state / "activated.json").exists())
            self.assertTrue((state / "auth.json").exists())
            self.assertTrue((state / "operator.yaml").exists())
            self.assertEqual((state / "secrets.env").stat().st_mode & 0o777, 0o600)
            self.assertTrue(request.session["authenticated"])
            self.assertIn("issued_at", request.session)
            self.assertEqual(
                json.loads((state / "activated.json").read_text())["version"], 1
            )

    def test_failed_activation_remains_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            request = SimpleNamespace(session={})
            with (
                patch.object(setup, "STATE_DIR", state),
                patch.object(setup, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(setup, "AUTH_FILE", state / "auth.json"),
                patch.object(setup, "setup_complete", return_value=False),
                patch.object(
                    setup, "reload_config", side_effect=RuntimeError("invalid config")
                ),
            ):
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.activate(
                        {
                            "password": "a sufficiently long password",
                            "profile": {},
                            "secrets": {},
                        },
                        request,
                    )
            self.assertEqual(raised.exception.status_code, 500)
            self.assertFalse((state / "activated.json").exists())
            self.assertFalse((state / "auth.json").exists())
            self.assertFalse((state / "operator.yaml").exists())

    def test_session_login_logout_and_html_redirect_flow(self):
        from fastapi.testclient import TestClient

        import main
        from routes.views import setup as setup_views

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
                patch.object(setup, "STATE_DIR", state),
                patch.object(setup, "AUTH_FILE", auth_file),
                patch.object(setup, "ACTIVATION_FILE", activation_file),
                patch.object(main, "STATE_DIR", state),
                patch.object(main, "AUTH_FILE", auth_file),
                patch.object(main, "OPERATOR_FILE", operator_file),
                patch.object(main, "ACTIVATION_FILE", activation_file),
                patch.object(main, "load_config", return_value=config),
                patch.object(setup_views, "STATE_DIR", state),
                patch.object(setup_views, "load_config", return_value=config),
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

                logout = client.post("/api/logout", headers={"x-csrf-token": csrf})
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


if __name__ == "__main__":
    unittest.main()
