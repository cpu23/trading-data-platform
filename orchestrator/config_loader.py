"""Configuration loading and validation for the orchestrator process.

Thin service wrapper around :class:`contracts.runtime_config.ConfigStore`:
loads ``/app/config/config.yaml`` plus the operator profile
(``operator.yaml``) and the private secrets file (``secrets.env``),
substitutes ``${ENV_VAR}`` references, and validates the merged result
against the frozen shared models.

Secret hygiene: the secrets file is re-read on every load and its values are
consulted *without* ever mutating the global ``os.environ``; once the file
exists, managed provider secrets are authoritative, so deleted or blanked
keys cannot linger or fall back to stale process-environment values.

In demo mode (``DEMO_MODE`` set) missing environment references resolve to
``"demo-disabled"`` and collectors/processors are disabled so the
credential-free demo can run without external dependencies.
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
    return cast(bool, demo_mode_enabled())


def _parse_yaml(path: str) -> object:
    with open(path) as handle:
        return yaml.safe_load(handle)


def _demo_transform(raw: ConfigMap) -> None:
    """Apply the shared offline demo policy before validation and hashing."""
    apply_demo_transform(raw)


def _missing_env_fallback(var_name: str) -> str | None:
    """Demo mode resolves missing environment references to a placeholder."""
    return cast(str | None, demo_missing_env_fallback(var_name))


def _resolve_config_path(config_path: str | None) -> str:
    return config_path or _DEFAULT_CONFIG_PATH


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
                missing_env_fallback=_missing_env_fallback,
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
]
