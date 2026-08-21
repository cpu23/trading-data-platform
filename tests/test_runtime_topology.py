import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = (ROOT / "docker-compose.yml", ROOT / "docker-compose.demo.yml")
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_runtime.sh"
# The exact eight-role production topology.
PRODUCTION_ROLES = frozenset(
    {"postgres", "migrate", "orchestrator", "scheduler", "worker", "outbox", "quotes", "api"}
)
# The six long-running application roles the deployment script (re)creates.
RUNTIME_ROLES = frozenset({"orchestrator", "scheduler", "worker", "outbox", "quotes", "api"})


def load_compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def executable_lines(path: Path) -> str:
    """The script with comment lines removed, so safety proofs cover the
    commands the shell will actually run, not the prose describing them."""
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


# Go duration units, as accepted by Compose's stop_grace_period. A loader
# that normalizes the value may emit integer nanoseconds instead of a string.
_DURATION_UNITS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}
_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h|d)")


def stop_grace_seconds(value) -> float:
    """stop_grace_period as a float number of seconds.

    Compose accepts Go-style duration strings ('60s', '30m', '1h30m'), and
    loaders that normalize durations may instead emit integer nanoseconds.
    Both representations resolve to the same semantic duration; a bare
    number string follows Go's ParseDuration convention (nanoseconds).
    """
    if isinstance(value, bool):
        raise TypeError(f"stop_grace_period must be a duration string or nanoseconds, got {value!r}")
    if isinstance(value, (int, float)):
        return value / 1_000_000_000
    if not isinstance(value, str):
        raise TypeError(f"stop_grace_period must be a duration string or nanoseconds, got {value!r}")
    if not value:
        raise ValueError("stop_grace_period must not be empty")
    if value.isdigit():
        return int(value) / 1_000_000_000
    seconds = 0.0
    position = 0
    for match in _DURATION_TOKEN.finditer(value):
        if match.start() != position:
            raise ValueError(f"invalid stop_grace_period duration: {value!r}")
        seconds += float(match.group(1)) * _DURATION_UNITS[match.group(2)]
        position = match.end()
    if position != len(value):
        raise ValueError(f"invalid stop_grace_period duration: {value!r}")
    return seconds


class RuntimeTopologyTests(unittest.TestCase):
    def test_single_image_contains_both_apps_and_has_neutral_default(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("api/pyproject.toml", dockerfile)
        self.assertIn("orchestrator/pyproject.toml", dockerfile)
        self.assertIn("COPY api /app/api", dockerfile)
        self.assertIn("COPY orchestrator /app/orchestrator", dockerfile)
        self.assertIn('CMD ["python3", "--version"]', dockerfile)
        self.assertNotIn("ENTRYPOINT", dockerfile)

    def test_production_and_demo_have_split_ordered_services(self):
        for path in COMPOSE_FILES:
            with self.subTest(path=path.name):
                services = load_compose(path)["services"]
                role_services = {"orchestrator", "scheduler", "worker", "outbox", "quotes"}
                expected = {"postgres", "migrate", "api", *role_services}
                image_services = {"migrate", "api", *role_services}
                if path.name == "docker-compose.demo.yml":
                    expected.add("demo-live")
                    image_services.add("demo-live")
                self.assertEqual(expected, set(services))
                for name in image_services:
                    self.assertEqual(services[name]["build"]["context"], ".")
                    self.assertEqual(services[name]["build"]["dockerfile"], "Dockerfile")
                    self.assertEqual(services[name]["image"], "trading-data-platform:0.1.0")
                    self.assertGreater(services[name]["pids_limit"], 0)
                    self.assertRegex(str(services[name]["mem_limit"]), r"^[0-9]+[mg]$")
                    self.assertIn("no-new-privileges:true", services[name]["security_opt"])

                self.assertEqual(services["migrate"]["restart"], "no")
                self.assertIn("cli.py", " ".join(services["migrate"]["command"]))
                self.assertIn("migrate", services["migrate"]["command"])
                self.assertEqual(
                    services["migrate"]["depends_on"]["postgres"]["condition"],
                    "service_healthy",
                )

                orchestrator = services["orchestrator"]
                self.assertIn("roles run api", " ".join(orchestrator["command"]))
                self.assertEqual(
                    orchestrator["depends_on"]["migrate"]["condition"],
                    "service_completed_successfully",
                )
                self.assertNotIn("ports", orchestrator)
                self.assertIn("healthcheck", orchestrator)
                for role in ("scheduler", "worker", "outbox", "quotes"):
                    service = services[role]
                    self.assertIn(f"roles run {role}", " ".join(service["command"]))
                    self.assertIn(f"check {role}", " ".join(service["healthcheck"]["test"]))
                    self.assertEqual(
                        service["depends_on"]["migrate"]["condition"],
                        "service_completed_successfully",
                    )
                    self.assertNotIn("ports", service)

                api = services["api"]
                self.assertIn("uvicorn", " ".join(api["command"]))
                self.assertEqual(
                    api["depends_on"]["orchestrator"]["condition"],
                    "service_healthy",
                )
                self.assertEqual(
                    api["depends_on"]["migrate"]["condition"],
                    "service_completed_successfully",
                )
                self.assertIn(
                    "http://orchestrator:8000",
                    api["environment"]["ORCHESTRATOR_URL"],
                )
                self.assertEqual(len(api["ports"]), 1)
                self.assertTrue(api["ports"][0].endswith(":8000"))

                if "demo-live" in services:
                    publisher = services["demo-live"]
                    self.assertIn("demo_live.py", " ".join(publisher["command"]))
                    self.assertEqual(publisher["restart"], "unless-stopped")
                    self.assertEqual(
                        publisher["depends_on"]["migrate"]["condition"],
                        "service_completed_successfully",
                    )
                    self.assertEqual(
                        api["depends_on"]["demo-live"]["condition"], "service_started"
                    )

    def test_upstream_images_are_immutable_and_runtime_is_non_root(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertGreaterEqual(dockerfile.count("@sha256:"), 3)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertNotIn(":latest", dockerfile)
        database_dockerfile = (ROOT / "db" / "Dockerfile").read_text()
        self.assertIn("timescale/timescaledb@sha256:", database_dockerfile)
        self.assertNotIn("timescaledb:latest", database_dockerfile)
        for path in COMPOSE_FILES:
            self.assertNotIn("timescaledb:latest", path.read_text())

    def test_image_artifacts_stay_root_owned_and_only_state_is_writable(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        # A compromised runtime user must not be able to modify application
        # code, config, prompts, or migrations shipped in the image.
        self.assertNotRegex(dockerfile, r"chown[^\n]*\s/app(\s|$)")
        self.assertIn("chown 10001:10001 /app/state", dockerfile)
        self.assertIn("chmod 700 /app/state", dockerfile)
        self.assertIn(
            "chown -R 10001:10001 /var/log/trading-data /var/lib/trading-data",
            dockerfile,
        )
        # Bytecode caching would write under the read-only /app tree; it is
        # disabled so imports never depend on a writable image layer.
        self.assertIn("ENV PYTHONDONTWRITEBYTECODE=1", dockerfile)

    def test_normal_compose_is_immutable_authenticated_and_internal_db(self):
        production = load_compose(ROOT / "docker-compose.yml")
        services = production["services"]
        self.assertNotIn("ports", services["postgres"])
        api_environment = services["api"]["environment"]
        self.assertEqual(api_environment["DEPLOYMENT_MODE"], "production")
        self.assertNotIn("DISABLE_AUTH", api_environment)
        for service_name, service in services.items():
            for volume in service.get("volumes", []):
                with self.subTest(service=service_name, volume=volume):
                    self.assertFalse(str(volume).startswith("."))

        development = load_compose(ROOT / "docker-compose.dev.yml")["services"]
        self.assertIn("ports", development["postgres"])
        self.assertTrue(
            any(
                str(volume).startswith(".")
                for service in development.values()
                for volume in service.get("volumes", [])
            )
        )

    def test_long_running_services_have_bounded_health_and_restart(self):
        for path in COMPOSE_FILES:
            services = load_compose(path)["services"]
            for name in ("orchestrator", "scheduler", "worker", "outbox", "quotes", "api"):
                with self.subTest(path=path.name, service=name):
                    service = services[name]
                    self.assertEqual(service["restart"], "unless-stopped")
                    health = service["healthcheck"]
                    for key in ("interval", "timeout", "retries", "start_period"):
                        self.assertIn(key, health)
                    probe = " ".join(health["test"])
                    if name in {"orchestrator", "api"}:
                        self.assertIn("http://127.0.0.1:8000", probe)
                    else:
                        self.assertIn(f"roles check {name}", probe)

    def test_operatorstate_is_readonly_everywhere_except_api_writer(self):
        """Immutable managed state: only the API setup/settings writer mounts
        /app/state read-write; migrate and every role mount it read-only.

        Production compose only — the demo compose is state-less by design
        (demo credentials are baked, no managed operator state).
        """
        services = load_compose(COMPOSE_FILES[0])["services"]
        for name in ("migrate", "orchestrator", "scheduler", "worker", "outbox", "quotes"):
            volumes = services[name].get("volumes", [])
            state_mounts = [v for v in volumes if "operatorstate" in v]
            self.assertEqual(
                len(state_mounts), 1, f"{name} must mount operatorstate"
            )
            self.assertTrue(
                state_mounts[0].endswith(":ro"),
                f"{name} must mount operatorstate read-only: {state_mounts[0]}",
            )
        api_volumes = services["api"].get("volumes", [])
        api_state = [v for v in api_volumes if "operatorstate" in v]
        self.assertEqual(len(api_state), 1, "api must mount operatorstate")
        self.assertFalse(
            api_state[0].endswith(":ro"),
            "api (setup/settings writer) must keep operatorstate rw",
        )

    def test_demo_is_offline_and_contains_no_secret_interpolation(self):
        demo = load_compose(ROOT / "docker-compose.demo.yml")
        rendered = (ROOT / "docker-compose.demo.yml").read_text()
        self.assertNotIn("${FRED_API_KEY", rendered)
        self.assertNotIn("${OPENROUTER_API_KEY", rendered)
        for name in (
            "migrate",
            "orchestrator",
            "scheduler",
            "worker",
            "outbox",
            "quotes",
            "demo-live",
            "api",
        ):
            environment = demo["services"][name]["environment"]
            self.assertEqual(environment["DEMO_MODE"], "true")
            self.assertEqual(environment["FRED_API_KEY"], "demo-disabled")
            self.assertEqual(environment["OANDA_API_KEY"], "demo-disabled")

    def test_demo_bootstrap_credentials_are_explicit_and_production_has_none(self):
        """The demo bootstrap is explicit and safe: demo services carry
        DEPLOYMENT_MODE=demo plus the non-secret demo/demo HTTP Basic
        credentials so a fresh volume authenticates at the root with no setup
        form. Production must not carry demo credentials or legacy auth."""
        demo = load_compose(ROOT / "docker-compose.demo.yml")
        for name in (
            "migrate",
            "orchestrator",
            "scheduler",
            "worker",
            "outbox",
            "quotes",
            "demo-live",
            "api",
        ):
            with self.subTest(service=name):
                environment = demo["services"][name]["environment"]
                self.assertEqual(environment["DEPLOYMENT_MODE"], "demo")
                self.assertEqual(environment["LEGACY_BASIC_AUTH"], "1")
                self.assertEqual(environment["DASHBOARD_USER"], "demo")
                self.assertEqual(environment["DASHBOARD_PASSWORD"], "demo")

        production = load_compose(ROOT / "docker-compose.yml")
        for name, service in production["services"].items():
            with self.subTest(service=name):
                environment = service.get("environment", {})
                self.assertNotIn("LEGACY_BASIC_AUTH", environment)
                self.assertNotIn("DASHBOARD_PASSWORD", environment)

    def test_production_compose_exposes_exactly_the_eight_expected_roles(self):
        production = load_compose(ROOT / "docker-compose.yml")
        self.assertEqual(PRODUCTION_ROLES, set(production["services"]))
        for role in RUNTIME_ROLES:
            with self.subTest(role=role):
                service = production["services"][role]
                self.assertEqual(service["restart"], "unless-stopped")
                self.assertIn("healthcheck", service)
                self.assertEqual(
                    service["depends_on"]["postgres"]["condition"],
                    "service_healthy",
                )
                self.assertEqual(
                    service["depends_on"]["migrate"]["condition"],
                    "service_completed_successfully",
                )

    def test_production_log_growth_is_bounded_and_outbox_drains_gracefully(self):
        production = load_compose(ROOT / "docker-compose.yml")
        for name, service in production["services"].items():
            with self.subTest(service=name):
                logging_config = service.get("logging")
                self.assertIsNotNone(logging_config, f"{name} must bound container logs")
                self.assertEqual(logging_config["driver"], "json-file")
                self.assertIn("max-size", logging_config["options"])
                self.assertIn("max-file", logging_config["options"])
        # Rollout recreation must not SIGKILL an in-flight outbox dispatch:
        # leases are 30s with bounded retry backoff, so 60s covers the
        # completion/lease-release boundary. The worker's job drain is longer.
        self.assertGreaterEqual(stop_grace_seconds(production["services"]["outbox"]["stop_grace_period"]), 30)
        self.assertGreaterEqual(stop_grace_seconds(production["services"]["worker"]["stop_grace_period"]), 1800)

    def test_live_contract_runner_asserts_exact_healthy_topology(self):
        source = (ROOT / "scripts" / "test_service_contracts.py").read_text()
        for marker in (
            "assert_topology",
            "PRODUCTION_ROLES",
            "HEALTHY_ROLES",
            "{{.Service}}|{{.State}}|{{.Health}}",
            "migrate one-shot exited",
            "missing expected roles",
        ):
            self.assertIn(marker, source)
        for role in PRODUCTION_ROLES:
            self.assertIn(role, source)

    def test_adr_records_ownership_ordering_storage_and_rollback(self):
        adr = (ROOT / "docs" / "adr" / "001-runtime-topology.md").read_text().lower()
        for term in (
            "single shell",
            "s6",
            "split",
            "process ownership",
            "service_completed_successfully",
            "shared storage",
            "rollback",
        ):
            self.assertIn(term, adr)


class DeploymentScriptSafetyTests(unittest.TestCase):
    """Static proofs that scripts/deploy_runtime.sh performs a bounded,
    idempotent, non-destructive rollout of exactly the expected roles."""

    def test_deploy_script_never_tears_down_or_removes_volumes(self):
        code = executable_lines(DEPLOY_SCRIPT)
        for forbidden in (
            "down",           # no teardown of any kind
            "volume rm",      # never removes volumes
            "--volumes",      # never uses the down -v / rm -v flags
            "prune",          # no unbounded docker system/image/builder pruning
            "rm -",           # no shell removal commands at all
            "--force-recreate",
        ):
            self.assertNotIn(forbidden, code, f"forbidden destructive token {forbidden!r}")
        self.assertIsNone(re.search(r"(^|\s)-v(\s|$)", code), "standalone -v flag forbidden")

    def test_deploy_script_builds_brings_up_postgres_and_awaits_migrate(self):
        script = DEPLOY_SCRIPT.read_text()
        self.assertTrue(script.startswith("#!/usr/bin/env bash"))
        self.assertIn("set -euo pipefail", script)
        self.assertIn("COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.yml}", script)
        self.assertIn("compose build", script)
        self.assertIn("up -d postgres", script)
        self.assertIn("await_healthy postgres", script)
        self.assertIn("run --rm migrate", script)
        self.assertIn("migrate completed successfully", script)
        self.assertIn("MAX_ATTEMPTS", script)
        self.assertIn('seq 1 "$MAX_ATTEMPTS"', script)

    def test_deploy_script_recreates_exactly_the_six_runtime_roles(self):
        script = DEPLOY_SCRIPT.read_text()
        match = re.search(r'RUNTIME_ROLES="([^"]+)"', script)
        self.assertIsNotNone(match, "RUNTIME_ROLES assignment missing")
        self.assertEqual(RUNTIME_ROLES, set(match.group(1).split()))
        # The recreate command uses --remove-orphans and nothing else that
        # could touch volumes, and the role list is the only up target.
        self.assertIn("up -d --remove-orphans $RUNTIME_ROLES", script)
        # Every runtime role is awaited individually; a missing or unhealthy
        # role makes the script fail.
        self.assertIn("for role in $RUNTIME_ROLES", script)
        self.assertIn('await_healthy "$role"', script)
        self.assertIn("fail_with_logs", script)
        self.assertIn("did not become healthy (missing or unhealthy)", script)

    def test_deploy_script_targets_match_the_compose_runtime_graph(self):
        production = load_compose(ROOT / "docker-compose.yml")
        compose_runtime = set(production["services"]) - {"postgres", "migrate"}
        match = re.search(r'RUNTIME_ROLES="([^"]+)"', DEPLOY_SCRIPT.read_text())
        self.assertIsNotNone(match)
        self.assertEqual(compose_runtime, set(match.group(1).split()))

    def test_deploy_script_prints_bounded_state_and_preserves_volumes(self):
        script = DEPLOY_SCRIPT.read_text()
        # Failure output is bounded: one ps snapshot plus a capped log tail.
        self.assertIn("compose ps", script)
        self.assertIn("--tail=100", script)
        self.assertIn("--- bounded service state ---", script)
        # The named volumes the rollout must preserve are named explicitly.
        for volume in ("pgdata", "newsdata", "logsdata", "operatorstate"):
            self.assertIn(volume, script)
        self.assertIn("preserving named volumes", script)


if __name__ == "__main__":
    unittest.main()
