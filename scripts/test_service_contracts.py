#!/usr/bin/env python3
"""Live demo cross-service contract acceptance using only stdlib and Docker."""

import argparse
import base64
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# The exact eight-role production topology.  Demo additionally runs demo-live.
PRODUCTION_ROLES = frozenset(
    {
        "postgres",
        "migrate",
        "orchestrator",
        "scheduler",
        "worker",
        "outbox",
        "quotes",
        "api",
    }
)
# Roles with a container health contract (migrate is a one-shot: it must have
# exited 0 instead of reporting healthy).
HEALTHY_ROLES = frozenset({"postgres", "orchestrator", "scheduler", "worker", "outbox", "quotes", "api"})
# Roles that must be running but expose no container healthcheck.
RUNNING_ROLES = frozenset({"demo-live"})


def request(base: str, path: str, auth: str | None = None, *, method: str = "GET", body=None):
    headers = {}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            if not raw:
                payload = None
            else:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = raw.decode("utf-8", errors="replace")
            return response.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = None
        return exc.code, payload


def compose(compose_file: str, *args: str) -> str:
    completed = subprocess.run(
        ["docker", "compose", "-f", compose_file, *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def docker_inspect(container: str, fmt: str) -> str:
    completed = subprocess.run(
        ["docker", "inspect", "--format", fmt, container],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def assert_topology(compose_file: str) -> None:
    """The running stack must expose exactly the expected roles: every
    long-running role healthy, one-shot migrate exited 0, nothing missing,
    and nothing extra running."""
    source = Path(compose_file).read_text()
    expected = PRODUCTION_ROLES
    if re.search(r"^  demo-live:", source, re.MULTILINE):
        expected = expected | RUNNING_ROLES

    rows = compose(compose_file, "ps", "-a", "--format", "{{.Service}}|{{.State}}|{{.Health}}")
    present = {line.split("|", 2)[0] for line in rows.splitlines() if line.strip()}
    missing = expected - present
    assert not missing, f"missing expected roles: {sorted(missing)}"
    extra = present - expected
    assert not extra, f"unexpected roles running: {sorted(extra)}"

    for service in sorted(expected & HEALTHY_ROLES):
        container = compose(compose_file, "ps", "-a", "-q", service).splitlines()[-1]
        status = docker_inspect(
            container,
            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
        )
        state, _, health = status.partition(" ")
        assert state == "running", f"{service} is not running: {status!r}"
        assert health == "healthy", f"{service} is not healthy: {status!r}"

    for service in sorted(expected & RUNNING_ROLES):
        container = compose(compose_file, "ps", "-a", "-q", service).splitlines()[-1]
        status = docker_inspect(container, "{{.State.Status}}")
        assert status == "running", f"{service} is not running: {status!r}"

    migrate = compose(compose_file, "ps", "-a", "-q", "migrate").splitlines()[-1]
    exit_code = docker_inspect(migrate, "{{.State.ExitCode}}")
    assert exit_code == "0", f"migrate one-shot exited {exit_code}"


def wait_for_api(base: str, auth: str, attempts: int = 40):
    for _ in range(attempts):
        try:
            status, payload = request(base, "/api/system/health", auth)
            if status == 200 and isinstance(payload, dict) and payload.get("readiness") in {"ready", "degraded"}:
                return payload
        except OSError:
            pass
        time.sleep(1)
    raise AssertionError("API did not recover with a ready health contract")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", default="docker-compose.demo.yml")
    parser.add_argument("--api-url", default="http://127.0.0.1:18080")
    parser.add_argument("--auth", default="demo:demo")
    args = parser.parse_args()

    assert_topology(args.compose_file)

    status, _ = request(args.api_url, "/api/system/health")
    assert status == 401, f"unauthenticated health returned {status}"

    health = wait_for_api(args.api_url, args.auth)
    assert health.get("liveness") == "ok"
    assert health.get("data_health") == "unknown", (
        "offline demo with no required collectors must report unknown data health"
    )

    status, _ = request(args.api_url, "/quality", args.auth)
    assert status == 200, f"quality page returned {status}"
    status, sources = request(args.api_url, "/api/news/sources", args.auth)
    assert status == 200 and isinstance(sources, dict) and isinstance(sources.get("sources"), list)
    status, clusters = request(args.api_url, "/api/news/clusters", args.auth)
    assert status in {200, 503}, f"canonical stories returned {status}"
    assert isinstance(clusters, dict), "canonical story contract is not an object"
    if status == 200:
        assert isinstance(clusters.get("clusters"), list)
        assert isinstance(clusters.get("lanes"), dict)

    status, _ = request(
        args.api_url,
        "/api/triggers/collectors/not-real",
        args.auth,
        method="POST",
        body={},
    )
    assert status == 404, f"invalid collector trigger returned {status}"

    stale_id = str(uuid.uuid4())
    sql = (
        "INSERT INTO cycle_runs "
        "(correlation_id,status,accepted_at,started_at,heartbeat_at,worker_id,triggered_by,run_kind,summary) VALUES "
        f"('{stale_id}','running',NOW()-INTERVAL '1 hour',NOW()-INTERVAL '1 hour',"
        "NOW()-INTERVAL '1 hour','dead-demo-worker','acceptance','cycle','{}'::jsonb);"
    )
    compose(args.compose_file, "exec", "-T", "postgres", "psql", "-U", "demo", "-d", "trading_data", "-v", "ON_ERROR_STOP=1", "-c", sql)
    compose(args.compose_file, "restart", "orchestrator")
    wait_for_api(args.api_url, args.auth)
    result = compose(
        args.compose_file,
        "exec", "-T", "postgres", "psql", "-U", "demo", "-d", "trading_data", "-Atc",
        f"SELECT status FROM cycle_runs WHERE correlation_id='{stale_id}'",
    )
    assert result == "abandoned", f"stale job reconciled as {result!r}"

    print("PASS: exact healthy topology, authentication, health, quality, News, trigger validation, and abandoned-job reconciliation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
