import hashlib
import json
import os
import posixpath
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from budgets import BudgetContext


def _canonical_value(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, set):
        return sorted((_canonical_value(item) for item in value), key=str)
    return value


def canonical_fingerprint(payload: dict) -> str:
    """Return a deterministic SHA-256 over canonical bounded metadata."""
    serialized = json.dumps(
        _canonical_value(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_prompt_template(template_path: str) -> tuple[str, dict[str, str]]:
    """Load one prompt and return only bounded, non-content identity metadata."""
    configured = os.fspath(template_path)
    config_root = Path(os.environ.get("CONFIG_DIR", "/app"))
    candidate = Path(configured)
    resolved = candidate if candidate.is_absolute() else config_root / candidate

    if not resolved.exists():
        raise FileNotFoundError(f"Prompt template not found: {resolved}")

    raw = resolved.read_bytes()
    normalized = posixpath.normpath(configured.replace("\\", "/"))
    if candidate.is_absolute():
        try:
            normalized = (
                resolved.resolve().relative_to(config_root.resolve()).as_posix()
            )
        except ValueError:
            path_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
            normalized = f"external/{candidate.name}:{path_hash}"

    return raw.decode("utf-8"), {
        "path": normalized,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


class Processor(Protocol):
    processor_id: str
    PROCESSOR_SCHEMA_VERSION: str

    def process(
        self,
        config: dict,
        correlation_id: str,
        budget_context: BudgetContext | None = None,
    ) -> dict:
        """Query raw data, construct prompt, call LLM, parse response into structured opinion."""
        ...

    def get_prompt_version(self) -> str:
        """Return version string for current prompt template."""
        ...

    def get_prompt_identity(self, config: dict) -> dict[str, str]:
        """Return safe configured-path and exact-content prompt markers."""
        ...

    def get_depends_on(self) -> list[str]:
        """Return list of collector source_ids this processor depends on."""
        ...

    def get_fingerprint_inputs(self, config: dict) -> dict:
        """Return bounded, deterministic markers for inputs actually consumed."""
        ...
