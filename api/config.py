"""Configuration loading and validation for the API process.

Thin service wrapper around :class:`contracts.runtime_config.ConfigStore`:
loads ``config/config.yaml`` plus the operator profile (``operator.yaml``)
and the private secrets file (``secrets.env``), substitutes ``${ENV_VAR}``
references, and validates the merged result against the frozen shared models.

Secret hygiene: the secrets file is re-read on every load and its values are
consulted *without* ever mutating the global ``os.environ``; once the file
exists, managed provider secrets are authoritative, so deleted or blanked
keys cannot linger or fall back to stale process-environment values.
"""

from __future__ import annotations

import os
from typing import Any, TypeAlias, cast

import yaml

from contracts.runtime_config import (
    AppConfig,
    ConfigError,
    ConfigSnapshot,
    ConfigStore,
    apply_demo_transform,
    committed_config_paths,
    demo_missing_env_fallback,
    demo_mode_enabled,
)

ConfigValue: TypeAlias = (
    str | int | float | bool | None | dict[str, "ConfigValue"] | list["ConfigValue"]
)
ConfigMap: TypeAlias = dict[str, Any]

_DEFAULT_CONFIG_PATH = "/app/config/config.yaml"
_DEFAULT_SECRETS_PATH = "/app/state/secrets.env"
_DEFAULT_OPERATOR_PATH = "/app/state/operator.yaml"

_store = ConfigStore()


def _demo_mode_enabled() -> bool:
    return demo_mode_enabled()


def _parse_yaml(path: str) -> object:
    with open(path) as handle:
        return yaml.safe_load(handle)


def _demo_transform(raw: ConfigMap) -> None:
    """Apply the same offline demo policy used by every runtime process."""
    apply_demo_transform(raw)


def _resolve_config_path(config_path: str | None) -> str:
    if config_path is not None:
        return config_path
    return os.environ.get("CONFIG_DIR", "/app/config") + "/config.yaml"


def _secrets_path() -> str:
    return os.environ.get("SECRETS_FILE", _DEFAULT_SECRETS_PATH)


def _operator_path() -> str:
    return os.environ.get("OPERATOR_CONFIG", _DEFAULT_OPERATOR_PATH)


def _locked_load(*, config_path: str, force: bool = False) -> AppConfig:
    """Load one committed state version under setup's root transaction lock."""
    with committed_config_paths(_operator_path(), _secrets_path()) as (
        operator_path,
        secrets_path,
    ):
        method = _store.reload if force else _store.load
        return cast(
            AppConfig,
            method(
                config_path=config_path,
                operator_path=operator_path,
                secrets_path=secrets_path,
                parse=_parse_yaml,
                demo_transform=_demo_transform,
                missing_env_fallback=demo_missing_env_fallback,
            ),
        )


def load_config(config_path: str | None = None) -> AppConfig:
    """Load and validate the effective configuration (cached by fingerprint)."""
    return _locked_load(config_path=_resolve_config_path(config_path))


def reload_config(config_path: str | None = None) -> AppConfig:
    """Invalidate the cache and load a fresh validated configuration."""
    return _locked_load(
        config_path=_resolve_config_path(config_path),
        force=True,
    )


def config_version() -> str | None:
    """Content-derived version of the active configuration snapshot."""
    return cast(str | None, _store.version())


def config_snapshot() -> ConfigSnapshot | None:
    """The active immutable configuration snapshot, if any."""
    return cast(ConfigSnapshot | None, _store.snapshot())


def config_status() -> dict[str, Any]:
    """Observability status: version, restart state, rejected reload candidate."""
    return cast(dict[str, Any], _store.status())


def restart_required() -> bool:
    """True when the latest reload changed a restart-sensitive section.

    The scheduler captures every job trigger and the config object at startup
    and durable workers retain that object, so schedule identity (collectors,
    processors, news sources, research, filings), LLM credentials, budget
    caps, the DB engine, log handlers, and worker singletons are all
    restart-sensitive.
    """
    return cast(bool, _store.restart_required())


def restart_sensitive_changes() -> list[str]:
    """Names of restart-sensitive sections changed by the latest reload."""
    return cast(list[str], _store.restart_changes())


def validate_candidate(
    operator_yaml: str,
    secrets_env: str,
    config_path: str | None = None,
) -> None:
    """Validate a staged operator/secrets candidate without touching state.

    Runs the full merge + substitution + AppConfig validation + credential
    gates on the candidate (secrets authoritative) against the live
    ``config.yaml``.  Raises :class:`ConfigError` (a ``ValueError``) on
    rejection; the store/cache/snapshots are never mutated, so a rejected
    commit leaves the prior live state untouched.
    """
    _store.validate_candidate(
        config_path=_resolve_config_path(config_path),
        operator_yaml=operator_yaml,
        secrets_env=secrets_env,
        parse=_parse_yaml,
        parse_text=lambda text: yaml.safe_load(text),
        demo_transform=_demo_transform,
        missing_env_fallback=demo_missing_env_fallback,
    )


__all__ = [
    "AppConfig",
    "ConfigError",
    "ConfigSnapshot",
    "config_snapshot",
    "config_version",
    "load_config",
    "reload_config",
    "restart_required",
    "restart_sensitive_changes",
    "validate_candidate",
]
