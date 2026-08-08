import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = (ROOT / "docker-compose.yml", ROOT / "docker-compose.demo.yml")


def load_compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


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
                expected = {"postgres", "migrate", "orchestrator", "api"}
                image_services = {"migrate", "orchestrator", "api"}
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
                self.assertIn("uvicorn", " ".join(orchestrator["command"]))
                self.assertEqual(
                    orchestrator["depends_on"]["migrate"]["condition"],
                    "service_completed_successfully",
                )
                self.assertNotIn("ports", orchestrator)
                self.assertIn("healthcheck", orchestrator)

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
                self.assertEqual(api["environment"]["ORCHESTRATOR_URL"], "http://orchestrator:8000")
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
        for path in COMPOSE_FILES:
            source = path.read_text()
            self.assertIn("timescale/timescaledb@sha256:", source)
            self.assertNotIn("timescaledb:latest", source)

    def test_long_running_services_have_bounded_health_and_restart(self):
        for path in COMPOSE_FILES:
            services = load_compose(path)["services"]
            for name in ("orchestrator", "api"):
                with self.subTest(path=path.name, service=name):
                    service = services[name]
                    self.assertEqual(service["restart"], "unless-stopped")
                    health = service["healthcheck"]
                    for key in ("interval", "timeout", "retries", "start_period"):
                        self.assertIn(key, health)
                    self.assertIn("http://127.0.0.1:8000", " ".join(health["test"]))

    def test_demo_is_offline_and_contains_no_secret_interpolation(self):
        demo = load_compose(ROOT / "docker-compose.demo.yml")
        rendered = (ROOT / "docker-compose.demo.yml").read_text()
        self.assertNotIn("${FRED_API_KEY", rendered)
        self.assertNotIn("${OPENROUTER_API_KEY", rendered)
        for name in ("migrate", "orchestrator", "demo-live", "api"):
            environment = demo["services"][name]["environment"]
            self.assertEqual(environment["DEMO_MODE"], "true")
            self.assertEqual(environment["FRED_API_KEY"], "demo-disabled")
            self.assertEqual(environment["OANDA_API_KEY"], "demo-disabled")

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


if __name__ == "__main__":
    unittest.main()
