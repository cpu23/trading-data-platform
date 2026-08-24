"""Run deterministic, upstream-free failure evidence with native unittest."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

DRILLS = [
    ("API DB unavailable", "api", "tests/test_phase11_security.py", "Phase11SecurityTests.test_api_database_unavailable_is_safe_unready_503"),
    ("orchestrator DB unavailable", "orchestrator", "tests/test_runtime_features.py", "HealthContractTests.test_health_returns_503_when_database_is_unavailable"),
    ("malformed config fail closed", "orchestrator", "tests/test_runtime_features.py", "RuntimeFeatureTests.test_config_env_substitution_names_absent_required_variable"),
    ("collector truthful partial/failed", "orchestrator", "tests/test_runtime_features.py", "CollectionFailureStatusTests"),
    ("LLM timeout safe telemetry", "orchestrator", "tests/test_llm_policy.py", "LLMStageDeadlineAndTelemetryTests.test_provider_error_completed_at_deadline_is_typed_timeout"),
    ("partial DB write retains spend", "orchestrator", "tests/test_llm_persistence.py", "ProcessorLLMPersistenceTests.test_partial_opinion_write_retains_exact_cumulative_llm_usage_once"),
    ("restart prerequisite", ".", "tests/test_process_supervision.py", "ProcessSupervisionTests.test_smoke_kills_each_service_and_asserts_restart_recovery"),
    ("concurrent cycle conflict", "orchestrator", "tests/test_runtime_features.py", "RuntimeFeatureTests.test_worker_lock_conflict_poisons_job_and_finalizes_run_failed"),
    ("news cursor unchanged", "orchestrator", "tests/test_news.py", "NewsTests.test_reuters_publication_failure_does_not_advance_cursor_and_retry_republishes"),
    ("migration checksum abort", "orchestrator", "tests/test_migrations.py", "MigrationInventoryTests.test_applied_checksum_mismatch_fails_before_pending_apply"),
]

RUNNER = r"""
import importlib.util
import io
import sys
import unittest

path, selector = sys.argv[1:]
spec = importlib.util.spec_from_file_location("failure_drill_target", path)
if spec is None or spec.loader is None:
    raise SystemExit(2)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
suite = unittest.defaultTestLoader.loadTestsFromName(selector, module)
if suite.countTestCases() == 0:
    raise SystemExit(2)
result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit-only", action="store_true", help="run deterministic unit evidence (default)")
    parser.add_argument("--docker", action="store_true", help="also run the existing Docker smoke")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    ok = True
    drill_env = dict(os.environ)
    for key in (
        "TRUSTED_HOSTS",
        "EXTERNAL_ORIGIN",
        "CSRF_SIGNING_KEY",
        "SESSION_SIGNING_KEY",
    ):
        drill_env.pop(key, None)
    for name, project, relative_path, selector in DRILLS:
        cwd = (root / project).resolve()
        interpreter = cwd / ".venv/bin/python"
        python = str(interpreter if interpreter.exists() else Path(sys.executable))
        result = subprocess.run(
            [python, "-c", RUNNER, str(cwd / relative_path), selector],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=drill_env,
        )
        passed = result.returncode == 0
        print(f"{'PASS' if passed else 'FAIL'} {name}")
        ok &= passed
    if args.docker:
        result = subprocess.run(
            ["bash", str(root / "scripts/smoke_test.sh")],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"{'PASS' if result.returncode == 0 else 'FAIL'} Docker smoke")
        ok &= result.returncode == 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
