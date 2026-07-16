import subprocess
import sys
from pathlib import Path


def test_failure_drills_unit_only_is_deterministic_and_successful():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/failure_drills.py", "--unit-only"],
        cwd=root, capture_output=True, text=True,
    )
    assert result.returncode == 0
    expected = {
        "API DB unavailable", "orchestrator DB unavailable", "malformed config fail closed",
        "collector truthful partial/failed", "LLM timeout safe telemetry",
        "partial DB write retains spend", "restart prerequisite", "concurrent cycle conflict",
        "news cursor unchanged", "migration checksum abort",
    }
    assert expected == {line[5:] for line in result.stdout.splitlines() if line.startswith("PASS ")}
    assert "FAIL" not in result.stdout
    assert result.stderr == ""


def test_failure_drills_docker_is_opt_in_and_invokes_existing_smoke(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/failure_drills.py"
    source = script.read_text()
    assert "--docker" in source
    assert "smoke_test.sh" in source
    assert "--unit-only" in source
