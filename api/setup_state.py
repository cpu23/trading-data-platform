"""Versioned, atomically committed private setup state.

Setup state is committed as immutable version snapshots under
``STATE_DIR/versions/v<N>``, each containing ``auth.json``, ``operator.yaml``,
``secrets.env`` and a ``manifest.json`` with content checksums.

A single symlink, ``STATE_DIR/current -> versions/v<N>``, is the atomic commit
point. The historical root paths are *stable* links through it:
``auth.json -> current/auth.json``, ``operator.yaml -> current/operator.yaml``,
``secrets.env -> current/secrets.env``, and the activation marker
``activated.json -> current/manifest.json``. Every consumer — the orchestrator's
``OPERATOR_CONFIG``/``SECRETS_FILE`` defaults, the API config loader, login,
settings and setup-complete validation — reads through ``current``, so one
``os.replace(current.tmp, current)`` switches all of them at once.

Failure and crash model
-----------------------
1. A new version directory is staged fully and validated before anything live
   changes; staging never touches the committed state.
2. The commit is the single atomic ``current`` swap. A failure at the swap
   leaves every consumer on the previous version.
3. A hard crash between staging and the swap leaves an orphaned (unreferenced)
   version directory; the next commit's version numbering skips it and pruning
   removes it.

Concurrency
-----------
All writers serialize on an exclusive ``flock`` on ``STATE_DIR/.setup.lock``.
``commit_setup`` REQUIRES the caller to hold that lock (it reads the pointer
and stages under it); callers acquire it around read-modify-commit. Readers
(``setup_complete``/``validate_committed_state``) do not lock; they only ever
observe one complete committed version.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from contracts.runtime_config import parse_secrets_file as contracts_parse_secrets_file

VERSIONS_DIRNAME = "versions"
LAYOUT = "versions"
LOCK_FILENAME = ".setup.lock"
CURRENT_LINK_NAME = "current"
SETUP_FILENAMES = ("auth.json", "operator.yaml", "secrets.env")

# Strict dotenv line: an environment-style name, then '=' and a printable value.

_SECRET_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

# The marker is always the current version manifest.
MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True)
class CommitResult:
    version: int
    directory: Path
    previous_version: int | None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------
# Layout helpers
# --------------------------------------------------------------------------


def versions_root(state_dir: Path) -> Path:
    return state_dir / VERSIONS_DIRNAME


def version_directory(state_dir: Path, version: int) -> Path:
    return versions_root(state_dir) / f"v{version}"


def current_link_path(state_dir: Path) -> Path:
    return state_dir / CURRENT_LINK_NAME


def read_pointer(marker_path: Path) -> dict | None:
    """Parse the current version manifest; ``None`` when absent or unreadable."""
    try:
        if not marker_path.exists():
            return None
        value = json.loads(marker_path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def pointer_version(pointer: dict | None) -> int | None:
    """Return a valid committed version number, else ``None``."""
    if not pointer:
        return None
    version = pointer.get("version")
    return version if isinstance(version, int) and version >= 1 else None


def is_versioned_pointer(pointer: dict | None) -> bool:
    return bool(pointer) and pointer.get("layout") == LAYOUT


def _highest_staged_version(state_dir: Path) -> int:
    root = versions_root(state_dir)
    if not root.is_dir():
        return 0
    highest = 0
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.startswith("v"):
            try:
                highest = max(highest, int(entry.name[1:]))
            except ValueError:
                continue
    return highest


# --------------------------------------------------------------------------
# Locking
# --------------------------------------------------------------------------


@contextmanager
def setup_lock(state_dir: Path):
    """Exclusive writer lock for setup-state commits (NOT reentrant per fd)."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_dir / LOCK_FILENAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --------------------------------------------------------------------------
# Content validation
# --------------------------------------------------------------------------


def _valid_auth_record(text: str) -> bool:
    try:
        record = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    salt = record.get("salt")
    digest = record.get("hash")
    if not isinstance(salt, str) or not isinstance(digest, str):
        return False
    if not salt or not digest:
        return False
    try:
        base64.b64decode(salt, validate=True)
        base64.b64decode(digest, validate=True)
    except (ValueError, TypeError):
        return False
    return True


def _valid_operator_profile(text: str) -> bool:
    try:
        profile = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return isinstance(profile, dict)


def _valid_secrets_file(text: str) -> bool:
    try:
        parse_secrets_file(text)
    except ValueError:
        return False
    return True


def parse_secrets_file(text: str) -> dict[str, str]:
    """Parse strict dotenv ``KEY=VALUE`` lines into a mapping.

    Shared strict implementation in :mod:`contracts.runtime_config` (names
    must be environment-style identifiers, values raw but control-char free,
    malformed lines and duplicate keys rejected).  Raises ``ValueError``.
    """
    return contracts_parse_secrets_file(text)


def validate_setup_contents(
    auth_json: str, operator_yaml: str, secrets_env: str
) -> None:
    """Raise ``ValueError`` when the payload is not a valid setup state."""
    if not _valid_auth_record(auth_json):
        raise ValueError("auth.json is not a valid password record")
    if not _valid_operator_profile(operator_yaml):
        raise ValueError("operator.yaml is not a valid YAML object")
    if not _valid_secrets_file(secrets_env):
        raise ValueError("secrets.env is not a valid dotenv file")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_version_directory(directory: Path, expected_version: int) -> bool:
    """Full validation of one committed version directory."""
    try:
        files = {name: (directory / name).read_text() for name in SETUP_FILENAMES}
    except OSError:
        return False
    manifest_path = directory / MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict):
        return False
    if manifest.get("version") != expected_version:
        return False
    recorded = manifest.get("files")
    if not isinstance(recorded, dict):
        return False
    for name in SETUP_FILENAMES:
        if recorded.get(name) != _sha256(files[name]):
            return False
    try:
        validate_setup_contents(
            files["auth.json"], files["operator.yaml"], files["secrets.env"]
        )
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------
# Live-state access
# --------------------------------------------------------------------------


def read_live_state(
    state_dir: Path, marker_path: Path, live_paths: dict[str, Path]
) -> dict[str, str]:
    """Read the complete current setup snapshot or return no state."""
    current_link = current_link_path(state_dir)
    if not current_link.is_symlink():
        return {}
    if all(path.is_symlink() and path.exists() for path in live_paths.values()):
        try:
            return {name: path.read_text() for name, path in live_paths.items()}
        except OSError:
            pass
    pointer = read_pointer(marker_path)
    version = pointer_version(pointer)
    if version is None or not is_versioned_pointer(pointer):
        return {}
    directory = version_directory(state_dir, version)
    try:
        return {name: (directory / name).read_text() for name in SETUP_FILENAMES}
    except OSError:
        return {}


# --------------------------------------------------------------------------
# Commit
# --------------------------------------------------------------------------


def _fsync_file(path: Path) -> None:
    """Durably persist one file; raises on real storage errors."""
    with open(path, "rb") as handle:
        os.fsync(handle.fileno())


# Directory fsync is unsupported on some filesystems; only these errnos are
# treated as "skip". Real storage failures (EIO, ENOSPC, ...) must propagate
# so a non-durable commit is never acknowledged.
_DIRECTORY_FSYNC_UNSUPPORTED = frozenset(
    {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), errno.EBADF}
)


def _fsync_dir(path: Path) -> None:
    """Fsync a directory so entry renames are durable.

    Skips only explicitly unsupported directory-fsync errnos (some
    filesystems reject O_DIRECTORY fsync); storage failures propagate.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno in _DIRECTORY_FSYNC_UNSUPPORTED:
            return
        raise


class CommitDurabilityError(OSError):
    """The pointer already flipped but its durability could not be confirmed.

    Raised only for failures AFTER the commit point; callers must not report
    the failure as a safe retry (the update is already live).
    """


def _write_private(path: Path, content: str) -> None:
    """Write a private file durably: temp -> fsync -> atomic replace -> dir fsync."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content)
    temporary.chmod(0o600)
    _fsync_file(temporary)
    os.replace(temporary, path)
    _fsync_file(path)
    _fsync_dir(path.parent)


def _stage_version(
    state_dir: Path,
    version: int,
    contents: dict[str, str],
    previous_version: int | None,
) -> Path:
    """Write and validate one version directory; never touches live state."""
    directory = version_directory(state_dir, version)
    if directory.exists():
        raise ValueError(f"version directory already exists: {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
        checksums = {name: _sha256(contents[name]) for name in SETUP_FILENAMES}
        for name in SETUP_FILENAMES:
            _write_private(directory / name, contents[name])
        manifest = {
            "version": version,
            "layout": LAYOUT,
            "committed_at": _iso_now(),
            "previous_version": previous_version,
            "files": checksums,
        }
        _write_private(directory / MANIFEST_FILENAME, json.dumps(manifest))
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    if not _validate_version_directory(directory, version):
        shutil.rmtree(directory, ignore_errors=True)
        raise ValueError(f"staged setup version v{version} failed validation")
    return directory


def _flip_current(current_link: Path, directory: Path) -> None:
    """Atomically point ``current`` at a version directory (the commit)."""
    temporary = current_link.with_name(f".{current_link.name}.tmp")
    try:
        temporary.unlink(missing_ok=True)
    except OSError:
        pass
    os.symlink(os.path.relpath(directory, current_link.parent), temporary)
    os.replace(temporary, current_link)


def _stable_link(path: Path, target: str) -> None:
    """Replace ``path`` with a symlink to ``target`` (e.g. ``current/auth.json``)."""
    temporary = path.with_name(f".{path.name}.link-tmp")
    try:
        temporary.unlink(missing_ok=True)
    except OSError:
        pass
    os.symlink(target, temporary)
    os.replace(temporary, path)


def _prune_versions(state_dir: Path, current: int, previous: int | None) -> None:
    """Best-effort retention: keep the current and the actual previous
    committed version (the rollback target recorded in the manifest)."""
    keep = {current}
    if previous is not None:
        keep.add(previous)
    root = versions_root(state_dir)
    try:
        for entry in root.iterdir():
            if not entry.is_dir() or not entry.name.startswith("v"):
                continue
            try:
                version = int(entry.name[1:])
            except ValueError:
                continue
            if version not in keep:
                shutil.rmtree(entry, ignore_errors=True)
    except OSError:
        pass


def commit_setup(
    state_dir: Path,
    marker_path: Path,
    live_paths: dict[str, Path],
    payload: dict[str, str],
    *,
    previous_version_hint: int | None = None,
    validate_candidate: Callable[[str, str], None] | None = None,
) -> CommitResult:
    """Commit a complete setup snapshot; the caller MUST hold ``setup_lock``.

    ``payload`` must contain exactly ``auth.json``, ``operator.yaml`` and
    ``secrets.env`` with their full new contents. The caller is responsible for
    merging against the current state (see :func:`read_live_state`) and for
    holding the writer lock around the whole read-modify-commit.

    ``validate_candidate(operator_yaml, secrets_env)``, when provided, runs
    the full shared configuration validation against the STAGED contents
    under the lock, before anything live changes; a rejection aborts the
    commit and the previously committed state stays live.

    The commit is a single atomic ``os.replace`` of the ``current`` symlink,
    made durable with fsyncs (staged files and the version directory before
    the flip; the state directory after). Raises ``ValueError`` for an
    invalid payload or rejected candidate and ``OSError`` for storage
    failures; on any failure the previously committed state stays live.
    """
    missing = set(SETUP_FILENAMES) - set(payload)
    if missing:
        raise ValueError(f"missing setup files in commit payload: {sorted(missing)}")
    validate_setup_contents(
        payload["auth.json"], payload["operator.yaml"], payload["secrets.env"]
    )

    current_link = current_link_path(state_dir)
    if not current_link.is_symlink():
        for name, path in live_paths.items():
            if not path.is_symlink():
                _stable_link(path, f"{CURRENT_LINK_NAME}/{name}")
        if not marker_path.is_symlink():
            _stable_link(marker_path, f"{CURRENT_LINK_NAME}/{MANIFEST_FILENAME}")
    # Repair consumer links before the atomic pointer flip.
    for name, path in live_paths.items():
        if not path.is_symlink():
            _stable_link(path, f"{CURRENT_LINK_NAME}/{name}")
    if not marker_path.is_symlink():
        _stable_link(marker_path, f"{CURRENT_LINK_NAME}/{MANIFEST_FILENAME}")

    # Re-read the pointer after repair so version numbering and pruning use
    # the true committed state (a tampered marker must not skew them).
    committed = pointer_version(read_pointer(marker_path))
    next_version = (
        max(
            committed or 0,
            previous_version_hint or 0,
            _highest_staged_version(state_dir),
        )
        + 1
    )
    directory = _stage_version(state_dir, next_version, payload, committed)

    # Full-configuration gate: the staged operator/secrets candidate must
    # validate before it can become current. ANY validation failure (model
    # rejection, substitution error, or a transiently invalid base config)
    # rejects the commit; nothing is committed and the prior state stays live.
    if validate_candidate is not None:
        try:
            validate_candidate(payload["operator.yaml"], payload["secrets.env"])
        except ValueError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise ValueError(f"candidate configuration is invalid: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - gate must fail closed
            shutil.rmtree(directory, ignore_errors=True)
            raise ValueError(
                f"candidate configuration could not be validated: {exc}"
            ) from exc

    # Durability: the staged version must be on stable storage before the
    # pointer flip; then flip atomically and persist the flip.
    _fsync_dir(versions_root(state_dir))
    _fsync_dir(state_dir)
    _flip_current(current_link, directory)
    try:
        _fsync_dir(state_dir)
    except OSError as exc:
        # The commit point already passed: the update is live but its
        # durability could not be confirmed. Distinct from pre-flip storage
        # failures so callers never report a safe retry.
        raise CommitDurabilityError(
            exc.errno, "setup state committed but durability could not be confirmed"
        ) from exc

    # The pointer flip switches every consumer to the new snapshot atomically.
    _prune_versions(state_dir, next_version, committed)
    return CommitResult(
        version=next_version,
        directory=directory,
        previous_version=committed,
    )


# --------------------------------------------------------------------------
# Committed-state validation (setup_complete)
# --------------------------------------------------------------------------


def validate_committed_state(
    state_dir: Path,
    marker_path: Path,
    live_paths: dict[str, Path],
) -> bool:
    """Validate the version pointer, manifest checksums, and stable consumer links."""
    pointer = read_pointer(marker_path)
    if pointer is None:
        return False
    if is_versioned_pointer(pointer):
        # Resolve the atomic pointer ONCE; everything else is validated
        # against the direct version files it names. A concurrent flip cannot
        # split the checks: the consumer links are stable (their readlink
        # targets never change) and are checked without re-traversing current.
        try:
            current_link = current_link_path(state_dir)
            if not current_link.is_symlink():
                return False
            resolved = current_link.resolve()
            if resolved.parent != versions_root(state_dir).resolve():
                return False
            if not resolved.name.startswith("v"):
                return False
            version = int(resolved.name[1:])
        except (OSError, ValueError):
            return False
        if version < 1:
            return False
        directory = version_directory(state_dir, version)
        if not _validate_version_directory(directory, version):
            return False
        # The marker and every consumer path must be the stable links (a
        # tampered real file or re-pointed link is not complete).
        try:
            if not _is_stable_link(
                marker_path, f"{CURRENT_LINK_NAME}/{MANIFEST_FILENAME}"
            ):
                return False
            for name, path in live_paths.items():
                if not _is_stable_link(path, f"{CURRENT_LINK_NAME}/{name}"):
                    return False
        except OSError:
            return False
        return True
    return False


def _is_stable_link(path: Path, target: str) -> bool:
    """True when ``path`` is a symlink whose target string is exactly
    ``target`` (e.g. ``current/auth.json``). readlink does not traverse
    ``current``, so concurrent pointer flips cannot split the check."""
    return path.is_symlink() and os.readlink(path) == target


def merge_profile(base: dict, update: dict) -> dict:
    """Deep-merge an operator profile update over the existing profile."""
    result = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_profile(result[key], value)
        else:
            result[key] = value
    return result
