#!/usr/bin/env python3
"""Measure the published investment dashboard API with cold and warm requests.

The command deliberately records observations rather than enforcing a latency
SLA.  ``--fixture`` starts a deterministic local HTTP server so CI and local
trend runs do not depend on PostgreSQL, provider APIs, or a deployed service.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import os
import platform
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError

RequestFn = Callable[[str, float, dict[str, str]], tuple[int, bytes]]


def percentile(samples: list[float], percent: float) -> float | None:
    """Return a linearly interpolated percentile, or ``None`` for no samples."""
    if not samples:
        return None
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (percent / 100)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _request_url(url: str, timeout: float, headers: dict[str, str]) -> tuple[int, bytes]:
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except HTTPError as exc:
        # HTTP failures are observations in the report, not an opaque command
        # crash.  Preserve any body because it helps diagnose API errors.
        return int(exc.code), exc.read()
    except (OSError, URLError) as exc:
        raise RuntimeError(f"request failed: {exc}") from exc


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "hostname": socket.gethostname(),
    }


def run_benchmark(
    url: str,
    *,
    warm_count: int = 5,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
    request_fn: RequestFn = _request_url,
    fixture: bool = False,
    fixture_items: int | None = None,
    dataset_cardinality: int | None = None,
    dataset_cardinality_source: str | None = None,
    scenario: str | None = None,
) -> dict[str, object]:
    """Run one cold request followed by sequential warm requests.

    ``request_fn`` is injectable for contract tests and remains a simple
    ``(url, timeout, headers) -> (status, body)`` callable.  Only successful
    2xx warm responses contribute latency percentiles; every failed request is
    retained in ``failures`` with its phase and request index.  Cardinality is
    supplied by the caller when it is known; response byte size is measured
    independently and never presented as cardinality.
    """
    if warm_count < 1:
        raise ValueError("warm_count must be at least 1")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    if dataset_cardinality is not None and dataset_cardinality < 0:
        raise ValueError("dataset_cardinality must not be negative")
    if dataset_cardinality is not None and not dataset_cardinality_source:
        raise ValueError("dataset_cardinality_source is required when cardinality is known")

    scenario_name = scenario or _default_scenario(url)
    request_headers = {"Accept": "application/json", **(headers or {})}
    failures: list[dict[str, object]] = []
    warm_samples: list[float] = []
    response_size: int | None = None
    cold: dict[str, object]

    def measure(phase: str, index: int) -> tuple[float, int | None, bytes | None]:
        started = time.perf_counter()
        try:
            status, body = request_fn(url, timeout, request_headers)
            latency = (time.perf_counter() - started) * 1000
            if not 200 <= status < 300:
                failures.append({
                    "phase": phase,
                    "index": index,
                    "kind": "http_status",
                    "status": status,
                    "message": f"HTTP {status}",
                })
            return latency, status, body
        except Exception as exc:  # request failures must be distinguishable in JSON
            latency = (time.perf_counter() - started) * 1000
            failures.append({
                "phase": phase,
                "index": index,
                "kind": "request_error",
                "error_type": type(exc).__name__,
                "message": str(exc),
            })
            return latency, None, None

    cold_latency, cold_status, cold_body = measure("cold", 0)
    cold_ok = cold_status is not None and 200 <= cold_status < 300
    if cold_ok and cold_body is not None:
        response_size = len(cold_body)
    cold = {
        "ok": cold_ok,
        "status": cold_status,
        "latency_ms": round(cold_latency, 3),
        "response_size_bytes": len(cold_body) if cold_ok and cold_body is not None else None,
    }

    for index in range(1, warm_count + 1):
        latency, status, body = measure("warm", index)
        if status is not None and 200 <= status < 300:
            warm_samples.append(latency)
            if body is not None:
                response_size = len(body)

    report: dict[str, object] = {
        "schema_version": 1,
        "scenario": scenario_name,
        "url": url,
        "timestamp": datetime.now(UTC).isoformat(),
        "environment": _environment(),
        "parameters": {
            "method": "GET",
            "warm_count_requested": warm_count,
            "timeout_seconds": timeout,
            "fixture": fixture,
            "fixture_items": fixture_items,
            "dataset_cardinality": dataset_cardinality,
            "dataset_cardinality_source": dataset_cardinality_source,
            "percentile_method": "linear_interpolation",
        },
        "cold": cold,
        "warm": {
            "count_requested": warm_count,
            "count_successful": len(warm_samples),
            "samples_ms": [round(sample, 3) for sample in warm_samples],
            "p50_ms": _rounded_percentile(warm_samples, 50),
            "p95_ms": _rounded_percentile(warm_samples, 95),
        },
        "dataset_cardinality": dataset_cardinality,
        "dataset_cardinality_source": dataset_cardinality_source,
        "response_size_bytes": response_size,
        "failures": failures,
    }
    return report


def _default_scenario(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.replace("/", "-")).strip("-")
    return label or "published-api"


def _rounded_percentile(samples: list[float], percent: float) -> float | None:
    value = percentile(samples, percent)
    return round(value, 3) if value is not None else None


def _fixture_payload(item_count: int) -> bytes:
    payload = {
        "model": "benchmark/fixture",
        "regions": [{"name": "global", "document_count": item_count}],
        "items": [
            {"id": index, "symbol": f"FIX{index:03d}", "value": index / 10}
            for index in range(item_count)
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class _FixtureHandler(BaseHTTPRequestHandler):
    server: "_FixtureServer"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path.split("?", 1)[0] != self.server.route_path:
            self.send_error(404, "fixture route not found")
            return
        if self.server.delay_seconds:
            time.sleep(self.server.delay_seconds)
        body = self.server.body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _FixtureServer(ThreadingHTTPServer):
    body: bytes
    delay_seconds: float
    route_path: str


@contextmanager
def fixture_server(
    item_count: int = 8,
    delay_ms: float = 0.0,
    route_path: str = "/api/investment/dashboard",
) -> Iterator[str]:
    """Serve a deterministic JSON payload on an ephemeral port."""
    if item_count < 0:
        raise ValueError("fixture_items must not be negative")
    if delay_ms < 0:
        raise ValueError("fixture_delay_ms must not be negative")
    if not route_path.startswith("/"):
        raise ValueError("fixture route_path must start with '/'")
    server = _FixtureServer(("127.0.0.1", 0), _FixtureHandler)
    server.body = _fixture_payload(item_count)
    server.delay_seconds = delay_ms / 1000
    server.route_path = route_path
    thread = threading.Thread(target=server.serve_forever, name="benchmark-fixture", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{route_path}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("BENCHMARK_URL", "http://127.0.0.1:8000/api/investment/dashboard"),
        help="Published API URL to measure (ignored as the target in --fixture mode).",
    )
    parser.add_argument("--warm-count", type=int, default=5, help="Sequential warm requests (default: 5).")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds.")
    parser.add_argument("--username", default=os.environ.get("DASHBOARD_USER"), help="Optional HTTP Basic username.")
    parser.add_argument("--password", default=os.environ.get("DASHBOARD_PASSWORD"), help="Optional HTTP Basic password.")
    parser.add_argument("--fixture", action="store_true", help="Use a deterministic local fixture server.")
    parser.add_argument(
        "--scenario",
        help="Machine-readable label for this route (default: URL path with '/' replaced by '-').",
    )
    parser.add_argument("--fixture-items", type=int, default=8, help="Number of deterministic fixture records.")
    parser.add_argument("--fixture-delay-ms", type=float, default=0.0, help="Optional deterministic fixture delay.")
    parser.add_argument("--output", type=Path, help="Also write JSON to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.warm_count < 1:
        raise SystemExit("--warm-count must be at least 1")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if args.fixture_items < 0:
        raise SystemExit("--fixture-items must not be negative")
    if args.fixture_delay_ms < 0:
        raise SystemExit("--fixture-delay-ms must not be negative")

    headers: dict[str, str] = {}
    if args.username is not None and args.password is not None:
        token = base64.b64encode(f"{args.username}:{args.password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    if args.fixture:
        route_path = urlsplit(args.url).path or "/api/investment/dashboard"
        with fixture_server(args.fixture_items, args.fixture_delay_ms, route_path) as fixture_url:
            report = run_benchmark(
                fixture_url,
                warm_count=args.warm_count,
                timeout=args.timeout,
                headers=headers,
                fixture=True,
                fixture_items=args.fixture_items,
                dataset_cardinality=args.fixture_items,
                dataset_cardinality_source="fixture_items",
                scenario=args.scenario,
            )
    else:
        report = run_benchmark(
            args.url,
            warm_count=args.warm_count,
            timeout=args.timeout,
            headers=headers,
            scenario=args.scenario,
        )

    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
