import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class CIWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text()
        cls.workflow = yaml.load(cls.source, Loader=yaml.BaseLoader)

    def test_push_and_pull_request_use_read_only_permissions(self):
        self.assertRegex(self.source, r"(?m)^on:\s*$")
        triggers = self.workflow["on"]
        self.assertIn("push", triggers)
        self.assertIn("pull_request", triggers)
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})

    def test_unit_jobs_use_python_312_uv_cache_and_exact_suites(self):
        jobs = self.workflow["jobs"]
        for name, directory in (("api-unit", "api"), ("orchestrator-unit", "orchestrator")):
            job = jobs[name]
            rendered = yaml.safe_dump(job)
            self.assertIn("actions/setup-python@v5", rendered)
            self.assertIn("astral-sh/setup-uv@v6", rendered)
            self.assertIn('python-version: \'3.12\'', rendered)
            self.assertIn("enable-cache: 'true'", rendered)
            commands = "\n".join(step.get("run", "") for step in job["steps"])
            self.assertIn("uv sync --frozen", commands)
            self.assertIn("uv run python -m unittest discover -s tests -v", commands)
            self.assertEqual(
                [step.get("working-directory") for step in job["steps"] if "unittest discover" in step.get("run", "")],
                [directory],
            )

    def test_static_job_covers_compile_migrations_compose_yaml_and_diff(self):
        commands = "\n".join(
            step.get("run", "") for step in self.workflow["jobs"]["static"]["steps"]
        )
        for required in (
            "python -m compileall",
            "tests/test_migrations.py",
            "docker compose config --quiet",
            "docker compose -f docker-compose.demo.yml config --quiet",
            "scripts/validate_fixtures.py",
            "git diff --check",
        ):
            self.assertIn(required, commands)

    def test_demo_smoke_is_secret_free_bounded_and_always_removes_volumes(self):
        job = self.workflow["jobs"]["demo-smoke"]
        rendered = yaml.safe_dump(job)
        self.assertNotIn("secrets.", rendered)
        self.assertIn("timeout-minutes", job)
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("scripts/smoke_test.sh", commands)
        teardown = [step for step in job["steps"] if step.get("if") == "always()"]
        self.assertEqual(len(teardown), 1)
        self.assertIn("down --volumes", teardown[0]["run"])
        self.assertIn("--remove-orphans", teardown[0]["run"])


if __name__ == "__main__":
    unittest.main()
