import os
import re

import yaml

_config_cache: dict | None = None
_config_cache_path: str | None = None
_config_cache_mtime_ns: int | None = None
_operator_cache_mtime_ns: int | None = None

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

def _load_private_environment():
    path = os.environ.get("SECRETS_FILE", "/app/state/secrets.env")
    if not os.path.exists(path):
        return
    with open(path) as secrets_file:
        for line in secrets_file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key] = value

def _merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        result = dict(base)
        for key, value in override.items():
            result[key] = _merge(result.get(key), value)
        return result
    return override


def _substitute_env_vars(value: str) -> str:
    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(
                f"Environment variable '{var_name}' referenced in config but not set"
            )
        return env_value

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _substitute_recursive(obj: object) -> object:
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _substitute_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_recursive(item) for item in obj]
    return obj


def load_config(config_path: str | None = None) -> dict:
    _load_private_environment()
    if config_path is None:
        config_path = os.environ.get("CONFIG_DIR", "/app/config") + "/config.yaml"

    global _config_cache, _config_cache_path, _config_cache_mtime_ns, _operator_cache_mtime_ns
    operator_path = os.environ.get("OPERATOR_CONFIG", "/app/state/operator.yaml")
    operator_mtime_ns = os.stat(operator_path).st_mtime_ns if os.path.exists(operator_path) else None
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    stat = os.stat(config_path)
    if (
        _config_cache is not None
        and _config_cache_path == config_path
        and _config_cache_mtime_ns == stat.st_mtime_ns
        and _operator_cache_mtime_ns == operator_mtime_ns
    ):
        return _config_cache

    with open(config_path) as f:
        raw_config = yaml.safe_load(f)
    if os.path.exists(operator_path):
        with open(operator_path) as operator_file:
            raw_config = _merge(raw_config, yaml.safe_load(operator_file) or {})

    config = _substitute_recursive(raw_config)
    if os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes"):
        config["demo"] = {"enabled": True}
    _config_cache = config
    _config_cache_path = config_path
    _config_cache_mtime_ns = stat.st_mtime_ns
    _operator_cache_mtime_ns = operator_mtime_ns
    return config


def reload_config(config_path: str | None = None) -> dict:
    global _config_cache, _config_cache_path, _config_cache_mtime_ns, _operator_cache_mtime_ns
    _config_cache = None
    _config_cache_path = None
    _config_cache_mtime_ns = None
    _operator_cache_mtime_ns = None
    return load_config(config_path)
