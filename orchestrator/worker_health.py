"""Lightweight stdlib-only health probe for the orchestrator worker process.

Emits and validates worker-local health state using an atomic status file.
Zero dependencies on SQLAlchemy, database drivers, or application config.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_HEALTH_FILE = Path("/tmp/worker_health.json")
DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_ALLOWED_CLOCK_SKEW_SECONDS = 5.0


def _get_process_start_time(pid: int) -> int | None:
    """Read process starttime from /proc/<pid>/stat (Linux)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            content = f.read()
        rparen = content.rfind(")")
        if rparen == -1:
            return None
        fields = content[rparen + 1 :].split()
        if len(fields) < 20 or fields[0] in {"Z", "X"}:
            return None
        return int(fields[19])
    except Exception:
        return None


def _get_boot_id() -> str | None:
    """Read host/container boot identity (Linux)."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as f:
            val = f.read().strip()
            return val if val else None
    except Exception:
        return None


def _is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is currently alive."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError, OverflowError, ValueError):
        return False


def clear_worker_health(path: Path | str | None = None) -> None:
    """Remove existing health status file."""
    target = Path(path) if path is not None else DEFAULT_HEALTH_FILE
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def write_worker_health(
    *,
    status: str,
    pid: int | None = None,
    path: Path | str | None = None,
) -> None:
    """Atomically write the worker health status to a local JSON file."""
    target = Path(path) if path is not None else DEFAULT_HEALTH_FILE
    current_pid = pid if pid is not None else os.getpid()

    payload = {
        "status": status,
        "pid": current_pid,
        "start_time": _get_process_start_time(current_pid),
        "boot_id": _get_boot_id(),
        "timestamp": time.time(),
    }

    data = json.dumps(payload, separators=(",", ":"))
    target.parent.mkdir(parents=True, exist_ok=True)

    temp_fd, temp_path = tempfile.mkstemp(
        prefix=f"{target.name}.tmp.",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(temp_path, target)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def check_worker_health(
    path: Path | str | None = None,
    timeout_seconds: float | None = None,
    allowed_clock_skew: float = DEFAULT_ALLOWED_CLOCK_SKEW_SECONDS,
) -> tuple[int, str]:
    """Validate worker health status from local file.

    Returns (exit_code, message) where 0 is healthy and 1 is unhealthy/error.
    """
    target = Path(path) if path is not None else DEFAULT_HEALTH_FILE

    timeout = (
        timeout_seconds if timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS
    )
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        return 1, f"invalid timeout: {timeout!r}"

    if (
        isinstance(allowed_clock_skew, bool)
        or not isinstance(allowed_clock_skew, (int, float))
        or not math.isfinite(allowed_clock_skew)
        or allowed_clock_skew < 0
    ):
        return 1, f"invalid allowed_clock_skew: {allowed_clock_skew!r}"

    if not target.exists():
        return 1, f"worker health file missing at {target}"

    try:
        content = target.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception as exc:
        return 1, f"worker health file invalid or unreadable: {type(exc).__name__}"

    if not isinstance(data, dict):
        return 1, "worker health payload is not a JSON object"

    # 1. Process identity & liveness
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return 1, f"invalid pid in worker health: {pid!r}"
    if not _is_pid_alive(pid):
        return 1, f"worker process pid {pid} is not alive"

    # 2. Host and process birth identity verification
    recorded_boot_id = data.get("boot_id")
    if (
        isinstance(recorded_boot_id, bool)
        or not isinstance(recorded_boot_id, str)
        or not recorded_boot_id.strip()
    ):
        return 1, f"invalid boot identity in worker health: {recorded_boot_id!r}"

    current_boot_id = _get_boot_id()
    if current_boot_id is None or not current_boot_id.strip():
        return 1, "cannot determine host boot identity"

    if recorded_boot_id != current_boot_id:
        return 1, "worker boot identity mismatch (system restarted)"

    recorded_start_time = data.get("start_time")
    if (
        isinstance(recorded_start_time, bool)
        or not isinstance(recorded_start_time, int)
        or recorded_start_time < 0
    ):
        return (
            1,
            f"invalid process start time in worker health: {recorded_start_time!r}",
        )

    current_start_time = _get_process_start_time(pid)
    if current_start_time is None:
        return 1, f"worker process pid {pid} is not alive"

    if current_start_time != recorded_start_time:
        return 1, f"worker process start time mismatch for pid {pid} (pid reused)"

    # 3. Timestamp freshness and clock skew
    ts = data.get("timestamp")
    if (
        isinstance(ts, bool)
        or not isinstance(ts, (int, float))
        or not math.isfinite(ts)
        or ts <= 0
    ):
        return 1, f"invalid timestamp in worker health: {ts!r}"

    now = time.time()
    age = now - ts
    if age < -allowed_clock_skew:
        return 1, f"worker health timestamp is in the future ({abs(age):.1f}s ahead)"

    if age > timeout:
        return 1, f"worker health is stale ({age:.1f}s old, limit {timeout:.1f}s)"

    # 4. Status check
    status = data.get("status")
    if status != "running":
        return 1, f"worker status is '{status}'"

    return 0, f"worker healthy (pid {pid}, age {max(0.0, age):.1f}s)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="worker_health",
        description="Lightweight worker-local health probe",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="path to worker health file (default: /tmp/worker_health.json)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="staleness window in seconds (default: 12.0)",
    )

    args = parser.parse_args(argv)

    code, msg = check_worker_health(
        path=args.path,
        timeout_seconds=args.timeout,
    )
    if code == 0:
        print(msg)
    else:
        print(msg, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
