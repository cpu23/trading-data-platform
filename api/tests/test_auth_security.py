import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import auth
from fastapi import HTTPException


class _Request:
    def __init__(
        self,
        path: str = "/api/system/health",
        *,
        accept: str = "application/json",
        session: dict | None = None,
        authorization: str | None = None,
    ):
        headers = {"accept": accept}
        if authorization is not None:
            headers["authorization"] = authorization
        self.headers = headers
        self.method = "GET"
        self.url = SimpleNamespace(path=path, query="")
        self.scope = {"session": session or {}}


class SessionAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.mode = patch.dict(
            os.environ,
            {
                "DEPLOYMENT_MODE": "test",
                "DISABLE_AUTH": "",
                "SESSION_SIGNING_KEY": "",
                "CSRF_SIGNING_KEY": "",
            },
            clear=False,
        )
        self.mode.start()

    def tearDown(self):
        self.mode.stop()

    def test_public_bootstrap_routes_do_not_require_session(self):
        for path in (
            "/setup",
            "/api/setup/status",
            "/api/setup/activate",
            "/login",
            "/api/login",
            "/ready",
            "/live",
        ):
            with self.subTest(path=path):
                self.assertEqual(auth.verify_credentials(_Request(path)), "bootstrap")

    def test_session_is_the_only_authenticated_request_state(self):
        self.assertEqual(
            auth.verify_credentials(_Request(session={"authenticated": True})),
            "admin",
        )
        with patch.object(auth, "setup_complete", return_value=True):
            with self.assertRaises(HTTPException) as raised:
                auth.verify_credentials(_Request(authorization="Basic dGVzdDp0ZXN0"))
        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("WWW-Authenticate", raised.exception.headers or {})

    def test_anonymous_html_redirects_to_login(self):
        with patch.object(auth, "setup_complete", return_value=True):
            with self.assertRaises(HTTPException) as raised:
                auth.verify_credentials(_Request("/quality", accept="text/html"))
        self.assertEqual(raised.exception.status_code, 303)
        self.assertEqual(raised.exception.headers["Location"], "/login?next=/quality")

    def test_password_hash_round_trip(self):
        encoded = auth.hash_password("correct horse battery staple")
        self.assertTrue(auth.verify_password("correct horse battery staple", encoded))
        self.assertFalse(auth.verify_password("wrong password", encoded))

    def test_session_and_csrf_tokens_are_tamper_evident(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(auth, "STATE_DIR", Path(directory)),
                patch.object(
                    auth,
                    "SESSION_SECRET_FILE",
                    Path(directory) / "session_secret",
                ),
            ):
                secret = auth.load_session_secret()
                session = auth.sign_session_cookie(
                    {"authenticated": True},
                    secret=secret,
                )
                self.assertTrue(
                    auth.decode_session_cookie(session, secret=secret)["authenticated"]
                )
                self.assertEqual(
                    auth.decode_session_cookie(session + "x", secret=secret),
                    {},
                )
                csrf = auth.mint_csrf_token()
                self.assertTrue(auth.verify_csrf_token(csrf))
                self.assertFalse(auth.verify_csrf_token(csrf + "x"))

    def test_disable_auth_is_limited_to_demo_and_test(self):
        with patch.dict(
            os.environ,
            {"DEPLOYMENT_MODE": "test", "DISABLE_AUTH": "true"},
            clear=False,
        ):
            self.assertEqual(auth.verify_credentials(_Request()), "admin")
        with patch.dict(
            os.environ,
            {"DEPLOYMENT_MODE": "production", "DISABLE_AUTH": "true"},
            clear=False,
        ):
            self.assertFalse(auth.auth_disabled())

    def test_production_requires_distinct_strong_signing_keys(self):
        key_a = "QX7Jc2VhM9pL4tR8wY3nK6dF1sZ5uB0gE7iO2aC9vN4"
        key_b = "Hm5Tq8Lr2Wz7Yp3Nc9Vx4Ka6Df1Gs0Ue8Bi2Oj5Pl7R"
        base = {
            "DEPLOYMENT_MODE": "production",
            "DISABLE_AUTH": "",
            "SESSION_SIGNING_KEY": key_a,
            "CSRF_SIGNING_KEY": key_b,
            "EXTERNAL_ORIGIN": "https://dashboard.example",
        }
        with patch.dict(os.environ, base, clear=False):
            auth.validate_signing_keys()
        with patch.dict(os.environ, {**base, "CSRF_SIGNING_KEY": key_a}, clear=False):
            with self.assertRaises(RuntimeError):
                auth.validate_signing_keys()


if __name__ == "__main__":
    unittest.main()
