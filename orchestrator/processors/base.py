import hashlib
import json
from datetime import date, datetime, timezone
from typing import Protocol

from budgets import BudgetContext


def _canonical_value(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
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
        _canonical_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


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

    def get_depends_on(self) -> list[str]:
        """Return list of collector source_ids this processor depends on."""
        ...

    def get_fingerprint_inputs(self, config: dict) -> dict:
        """Return bounded, deterministic markers for inputs actually consumed."""
        ...
