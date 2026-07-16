import base64
import os
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(DASHBOARD_USER="test", DASHBOARD_PASSWORD="test")

with patch("config.load_config", return_value={"logging": {"level": "INFO"}}):
    from main import create_app  # noqa: E402


class Upstream:
    async def get(self, url, **kwargs):
        return httpx.Response(200, json={"EURUSD": 1}, request=httpx.Request("GET", url))
    async def post(self, url, **kwargs):
        return httpx.Response(202, json={"job_id": "x"}, request=httpx.Request("POST", url))
    async def aclose(self): pass


def app():
    return create_app(orchestrator_client_factory=lambda **_: Upstream())


AUTH = {"Authorization": "Basic " + base64.b64encode(b"test:test").decode()}


def test_mutation_forwards_basic_and_sse_requires_signed_token():
    with TestClient(app()) as client:
        assert client.post("/api/collect/fred").status_code == 401
        response = client.post("/api/collect/fred", headers=AUTH)
        assert response.status_code == 202
        token = client.get("/api/quotes/stream-token", headers=AUTH).json()["token"]
        assert client.get("/api/quotes/stream").status_code == 401
        assert client.get("/api/quotes/stream", params={"token": token}, headers=AUTH).status_code == 200
        assert client.get("/api/quotes/stream", params={"token": token}, headers=AUTH).status_code == 401


def test_browser_mutation_requires_csrf_but_json_machine_call_is_exempt():
    with TestClient(app()) as client:
        client.get("/", headers=AUTH)
        assert client.post("/api/settings/timezone", json={"timezone": "UTC"}, headers=AUTH).status_code == 403
        token = client.cookies.get("__Host-csrf")
        headers = {**AUTH, "X-CSRF-Token": token, "Origin": "http://testserver"}
        assert client.post("/api/settings/timezone", json={"timezone": "UTC"}, headers=headers).status_code == 200
