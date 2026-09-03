#!/usr/bin/env python3
"""Run unittest with a host-protecting address-space limit."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

DEFAULT_MEMORY_LIMIT_BYTES = 4 * 1024**3
MEMORY_LIMIT_ENV = "TEST_MEMORY_LIMIT_BYTES"


def memory_limit_bytes(environ: Mapping[str, str]) -> int:
    """Return the configured positive byte limit."""
    raw = environ.get(MEMORY_LIMIT_ENV, str(DEFAULT_MEMORY_LIMIT_BYTES))
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError(f"{MEMORY_LIMIT_ENV} must be a positive byte count") from exc
    if limit <= 0:
        raise ValueError(f"{MEMORY_LIMIT_ENV} must be a positive byte count")
    return limit


def apply_memory_limit(limit: int) -> bool:
    """Apply RLIMIT_AS where supported; return whether the process is bounded."""
    if os.name != "posix":
        print(
            f"warning: {MEMORY_LIMIT_ENV} is unsupported on {sys.platform}; "
            "tests are unbounded",
            file=sys.stderr,
        )
        return False

    import resource

    if not hasattr(resource, "RLIMIT_AS"):
        print(
            f"warning: RLIMIT_AS is unavailable on {sys.platform}; tests are unbounded",
            file=sys.stderr,
        )
        return False

    _, hard_limit = resource.getrlimit(resource.RLIMIT_AS)
    effective_limit = (
        limit if hard_limit == resource.RLIM_INFINITY else min(limit, hard_limit)
    )
    resource.setrlimit(resource.RLIMIT_AS, (effective_limit, effective_limit))
    return True


def main(argv: Sequence[str] | None = None) -> None:
    """Apply the limit, then replace this process with unittest."""
    try:
        apply_memory_limit(memory_limit_bytes(os.environ))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot bound test memory: {exc}") from exc

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        args = ["discover", "-s", "tests", "-v"]
    os.execv(sys.executable, [sys.executable, "-m", "unittest", *args])


if __name__ == "__main__":
    main()
