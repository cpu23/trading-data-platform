from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class CollectionResult:
    """Result of a collector run, carrying records and per-source errors.

    Central contract between collectors and the orchestrator's status derivation.
    """

    records: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    total_series: int = 0
    successful_series: int = 0

    @property
    def all_failed(self) -> bool:
        return self.total_series > 0 and self.successful_series == 0

    @property
    def partial_failure(self) -> bool:
        return self.total_series > 0 and 0 < self.successful_series < self.total_series


class Collector(Protocol):
    source_id: str

    def collect(self, config: dict, correlation_id: str) -> "CollectionResult | list[dict]":
        """Fetch and normalise data. Returns CollectionResult or list of dicts matching target table schema."""
        ...

    def get_schedule(self, config: dict) -> str:
        """Return cron expression from config."""
        ...

    def health_check(self, config: dict) -> dict:
        """Check upstream API reachability. Returns {healthy: bool, message: str, latency_ms: int}."""
        ...

    def get_target_table(self) -> str:
        """Return the database table name this collector writes to."""
        ...

    def get_conflict_columns(self) -> list[str]:
        """Return the columns used for upsert conflict detection."""
        ...
