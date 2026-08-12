import base64
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(
    DASHBOARD_USER="test", DASHBOARD_PASSWORD="test", LEGACY_BASIC_AUTH="1"
)
os.environ["STATE_DIR"] = "/tmp/test_state"
os.environ["DEPLOYMENT_MODE"] = "test"

with patch("config.load_config", return_value={"logging": {"level": "INFO"}}):
    from main import create_app  # noqa: E402
from auth import mint_sse_token, verify_sse_token  # noqa: E402


class Upstream:
    async def get(self, url, **kwargs):
        return httpx.Response(
            200, json={"EURUSD": 1}, request=httpx.Request("GET", url)
        )

    async def post(self, url, **kwargs):
        return httpx.Response(
            202,
            json={"job_id": "x", "accepted_at": "2026-08-04T00:00:00Z"},
            request=httpx.Request("POST", url),
        )

    async def aclose(self):
        pass


def make_app():
    return create_app(orchestrator_client_factory=lambda **_: Upstream())


AUTH = {"Authorization": "Basic " + base64.b64encode(b"test:test").decode()}


async def finite_events(_request):
    yield "data: {}\n\n"


class Phase11SecurityTests(unittest.TestCase):
    def test_every_public_mutation_family_requires_basic_auth(self):
        with TestClient(make_app(), base_url="https://testserver") as client:
            paths = (
                "/api/collect/fred",
                "/api/process/macro_regime",
                "/api/triggers/news/reuters",
                "/api/triggers/cycle",
                "/api/settings/timezone",
            )
            for path in paths:
                with self.subTest(path=path):
                    response = client.post(path, json={})
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers.get("www-authenticate"), "Basic")

    def test_sse_tokens_are_expiring_path_bound_and_tamper_evident(self):
        self.assertFalse(verify_sse_token(mint_sse_token(ttl=-1), "/api/quotes/stream"))
        self.assertFalse(
            verify_sse_token(mint_sse_token(path="/wrong"), "/api/quotes/stream")
        )
        token = mint_sse_token()
        self.assertFalse(verify_sse_token(token + "tampered", "/api/quotes/stream"))

    def test_api_database_unavailable_is_safe_unready_503(self):
        with TestClient(make_app(), base_url="https://testserver") as client:
            with patch(
                "routes.json.system.query_many",
                side_effect=RuntimeError("RAW_DB_SECRET"),
            ):
                response = client.get("/api/system/health", headers=AUTH)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["liveness"], "ok")
        self.assertEqual(response.json()["readiness"], "unready")
        self.assertNotIn("RAW_DB_SECRET", response.text)

    def test_mutation_forwards_basic_and_sse_uses_short_lived_signed_cookie(self):
        with TestClient(make_app(), base_url="https://testserver") as client:
            self.assertEqual(client.post("/api/collect/fred").status_code, 401)
            self.assertEqual(
                client.post("/api/collect/fred", headers=AUTH, json={}).status_code, 202
            )
            with patch("routes.json.watchlist._quote_events") as events:
                self.assertEqual(client.get("/api/quotes/stream").status_code, 401)
                events.assert_not_called()
            token_response = client.get("/api/quotes/stream-token", headers=AUTH)
            self.assertEqual(token_response.status_code, 200)
            self.assertNotIn("token", token_response.json())
            self.assertIsNotNone(client.cookies.get("sse-auth"))
            sse_cookie = next(
                value
                for value in token_response.headers.get_list("set-cookie")
                if value.startswith("sse-auth=")
            )
            self.assertIn("HttpOnly", sse_cookie)
            self.assertIn("SameSite=strict", sse_cookie)
            self.assertIn("Path=/api/quotes/stream", sse_cookie)
            self.assertIn("Max-Age=60", sse_cookie)
            with patch("routes.json.watchlist._quote_events", finite_events):
                response = client.get("/api/quotes/stream")
                self.assertEqual(response.status_code, 200)
                self.assertIn("data: {}", response.text)
            with patch("routes.json.watchlist._quote_events", finite_events):
                self.assertEqual(client.get("/api/quotes/stream").status_code, 200)

    def test_sse_cookie_secure_flag_is_configurable(self):
        with patch.dict(os.environ, {"COOKIE_SECURE": "1"}):
            with TestClient(make_app(), base_url="https://testserver") as client:
                response = client.get("/api/quotes/stream-token", headers=AUTH)
        sse_cookie = next(
            value
            for value in response.headers.get_list("set-cookie")
            if value.startswith("sse-auth=")
        )
        self.assertIn("Secure", sse_cookie)

    def test_sse_cookie_rejects_expired_and_wrong_path_before_generator(self):
        with TestClient(make_app(), base_url="https://testserver") as client:
            for token in (mint_sse_token(ttl=-1), mint_sse_token(path="/wrong")):
                client.cookies.set("sse-auth", token, path="/api/quotes/stream")
                with patch("routes.json.watchlist._quote_events") as events:
                    response = client.get("/api/quotes/stream")
                self.assertEqual(response.status_code, 401)
                events.assert_not_called()

    def test_browser_mutation_requires_csrf_but_json_machine_call_is_exempt(self):
        with TestClient(make_app(), base_url="https://testserver") as client:
            self.assertEqual(
                client.post(
                    "/api/settings/timezone", json={"timezone": "UTC"}, headers=AUTH
                ).status_code,
                200,
            )
            self.assertEqual(
                client.get(
                    "/quality",
                    headers={**AUTH, "Origin": "https://testserver"},
                ).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    "/api/settings/timezone", json={"timezone": "UTC"}, headers=AUTH
                ).status_code,
                403,
            )
            token = client.cookies.get("csrf-token")
            self.assertIsNotNone(token)
            headers = {**AUTH, "X-CSRF-Token": token, "Origin": "https://testserver"}
            self.assertEqual(
                client.post(
                    "/api/settings/timezone", json={"timezone": "UTC"}, headers=headers
                ).status_code,
                200,
            )
            bad_token = {
                **AUTH,
                "X-CSRF-Token": "invalid",
                "Origin": "https://testserver",
            }
            self.assertEqual(
                client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers=bad_token,
                ).status_code,
                403,
            )
            cross_origin = {
                **AUTH,
                "X-CSRF-Token": token,
                "Origin": "https://attacker.invalid",
            }
            self.assertEqual(
                client.post(
                    "/api/settings/timezone",
                    json={"timezone": "UTC"},
                    headers=cross_origin,
                ).status_code,
                403,
            )


if __name__ == "__main__":
    unittest.main()
