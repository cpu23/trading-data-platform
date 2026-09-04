#!/usr/bin/env python3
"""Live acceptance checks for the three-service demo runtime."""

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

EXPECTED_SERVICES = frozenset({"postgres", "web", "worker"})


def request(base: str, path: str, *, method: str = "GET", body=None):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            try:
                payload = json.loads(raw) if raw else None
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
    source = Path(compose_file).read_text()
    assert "services:" in source
    rows = compose(
        compose_file, "ps", "-a", "--format", "{{.Service}}|{{.State}}|{{.Health}}"
    )
    present = {line.split("|", 2)[0] for line in rows.splitlines() if line.strip()}
    assert present == EXPECTED_SERVICES, (
        f"expected exactly {sorted(EXPECTED_SERVICES)}, got {sorted(present)}"
    )
    for service in sorted(EXPECTED_SERVICES):
        container = compose(compose_file, "ps", "-a", "-q", service).splitlines()[-1]
        status = docker_inspect(
            container,
            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
        )
        state, _, health = status.partition(" ")
        assert state == "running", f"{service} is not running: {status!r}"
        assert health == "healthy", f"{service} is not healthy: {status!r}"


def wait_for_web(base: str, attempts: int = 40):
    for _ in range(attempts):
        try:
            status, payload = request(base, "/api/system/health")
            if status == 200 and isinstance(payload, dict):
                return payload
        except OSError:
            pass
        time.sleep(1)
    raise AssertionError("web did not recover with a health contract")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", default="docker-compose.demo.yml")
    parser.add_argument("--api-url", default="http://127.0.0.1:18080")
    args = parser.parse_args()

    assert_topology(args.compose_file)
    health = wait_for_web(args.api_url)
    assert health.get("liveness") == "ok"

    status, _ = request(args.api_url, "/quality")
    assert status == 200, f"quality page returned {status}"
    status, sources = request(args.api_url, "/api/news/sources")
    assert status == 200 and isinstance(sources, dict)
    assert isinstance(sources.get("sources"), list)
    status, clusters = request(args.api_url, "/api/news/clusters")
    assert status in {200, 503}, f"canonical stories returned {status}"
    assert isinstance(clusters, dict), "canonical story contract is not an object"

    status, topology = request(args.api_url, "/api/system/topology")
    assert status == 200 and isinstance(topology, dict)
    node_ids = {node["id"] for node in topology.get("nodes", [])}
    assert node_ids == EXPECTED_SERVICES, (
        f"unexpected topology nodes: {sorted(node_ids)}"
    )

    status, _ = request(
        args.api_url,
        "/api/triggers/collectors/not-real",
        method="POST",
        body={},
    )
    assert status == 404, f"invalid collector trigger returned {status}"

    print(
        "PASS: exact three-service topology, health, quality, News, and trigger contracts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
