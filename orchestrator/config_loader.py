import os
import re

import yaml

_config_cache: dict | None = None
_config_cache_path: str | None = None
_config_cache_mtime_ns: int | None = None

_ENV_VAR_PATTERN = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"
)


def _substitute_env_vars(value: str) -> str:
    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2)
        if var_name in os.environ:
            value = os.environ[var_name]
            if value or default is not None:
                return value
            raise ValueError(
                f"Environment variable '{var_name}' referenced in config must not be empty"
            )
        if default is not None:
            return default
        if os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes"):
            return "demo-disabled"
        raise ValueError(
            f"Environment variable '{var_name}' referenced in config but not set"
        )

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _substitute_recursive(obj: object) -> object:
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _substitute_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_recursive(item) for item in obj]
    return obj


def load_config(config_path: str = "/app/config/config.yaml") -> dict:
    global _config_cache, _config_cache_path, _config_cache_mtime_ns
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    stat = os.stat(config_path)
    if (
        _config_cache is not None
        and _config_cache_path == config_path
        and _config_cache_mtime_ns == stat.st_mtime_ns
    ):
        return _config_cache

    with open(config_path) as f:
        raw_config = yaml.safe_load(f)

    config = _substitute_recursive(raw_config)
    if os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes"):
        config["demo"] = {"enabled": True}
        for item in config.get("collectors", {}).values():
            item["enabled"] = False
        for item in config.get("processors", {}).values():
            item["enabled"] = False
    _config_cache = config
    _config_cache_path = config_path
    _config_cache_mtime_ns = stat.st_mtime_ns
    return config


def reload_config(config_path: str = "/app/config/config.yaml") -> dict:
    global _config_cache, _config_cache_path, _config_cache_mtime_ns
    _config_cache = None
    _config_cache_path = None
    _config_cache_mtime_ns = None
    return load_config(config_path)
