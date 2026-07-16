"""Run the deterministic, upstream-free failure evidence suite."""
import argparse
import subprocess
import sys
from pathlib import Path

DRILLS = [
    ("API DB unavailable", "api/tests/test_routes.py::TestSystemRoutes::test_malformed_complete_orchestrator_health_contract_returns_503"),
    ("orchestrator DB unavailable", "orchestrator/tests/test_runtime_features.py::HealthContractTests::test_health_returns_503_when_database_is_unavailable"),
    ("malformed config fail closed", "orchestrator/tests/test_runtime_features.py::RuntimeFeatureTests::test_config_env_substitution_names_absent_required_variable"),
    ("collector truthful partial/failed", "orchestrator/tests/test_runtime_features.py::CollectionFailureStatusTests"),
    ("LLM timeout safe telemetry", "orchestrator/tests/test_llm_policy.py::LLMStageDeadlineAndTelemetryTests::test_provider_error_completed_at_deadline_is_typed_timeout"),
    ("partial DB write retains spend", "orchestrator/tests/test_llm_persistence.py::ProcessorLLMPersistenceTests::test_partial_opinion_write_retains_exact_cumulative_llm_usage_once"),
    ("restart prerequisite", "tests/test_process_supervision.py::ProcessSupervisionTests::test_smoke_kills_each_service_and_asserts_restart_recovery"),
    ("concurrent cycle conflict", "orchestrator/tests/test_runtime_features.py::RuntimeFeatureTests::test_background_lock_conflict_finalizes_stable_failed_result"),
    ("news cursor unchanged", "orchestrator/tests/test_news.py::NewsTests::test_reuters_publication_failure_does_not_advance_cursor_and_retry_republishes"),
    ("migration checksum abort", "orchestrator/tests/test_migrations.py::MigrationApplicationTests::test_applied_checksum_mismatch_fails_before_pending_apply"),
]

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit-only", action="store_true", help="run deterministic unit evidence (default)")
    parser.add_argument("--docker", action="store_true", help="also run the existing Docker smoke")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    ok = True
    for name, selector in DRILLS:
        cwd = root / ("api" if selector.startswith("api/") else "orchestrator" if selector.startswith("orchestrator/") else ".")
        interpreter = cwd / ".venv/bin/python"
        module_selector, *node = selector.split("::", 1)
        python = str(interpreter if interpreter.exists() else Path(sys.executable))
        probe = subprocess.run([python, "-c", "import pytest"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode == 0:
            target = str(root / module_selector) + (f"::{node[0]}" if node else "")
            command = [python, "-m", "pytest", "-q", target]
        else:
            module_name = module_selector[:-3].replace("/", ".") if module_selector.endswith(".py") else module_selector.replace("/", ".")
            command = [python, "-m", "unittest", f"{module_name}{'.' + node[0] if node else ''}"]
        result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        passed = result.returncode == 0
        print(f"{'PASS' if passed else 'FAIL'} {name}")
        ok &= passed
    if args.docker:
        result = subprocess.run([str(root / "scripts/smoke_test.sh")], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        print(f"{'PASS' if result.returncode == 0 else 'FAIL'} Docker smoke")
        ok &= result.returncode == 0
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
