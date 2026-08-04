import time
from dataclasses import dataclass, field
from typing import Protocol


def elapsed_ms(started_at: float) -> int:
    """Return a nonnegative elapsed duration rounded to the nearest millisecond."""
    return max(0, round((time.monotonic() - started_at) * 1000))


@dataclass
class CollectionResult:
    """Result of a collector run, carrying records and per-source errors.

    Central contract between collectors and the orchestrator's status derivation.
    """

    records: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    total_series: int = 0
    successful_series: int = 0
    metrics: dict[str, int] = field(default_factory=dict)

    @property
    def all_failed(self) -> bool:
        return self.total_series > 0 and self.successful_series == 0

    @property
    def partial_failure(self) -> bool:
        return self.successful_series > 0 and bool(self.errors)


class CollectorStateError(RuntimeError):
    """Expected collector state that must not be reported as a healthy success."""

    state = "failed"

    def __init__(self, message: str, **metadata):
        super().__init__(f"{self.state}: {message}")
        self.metadata = {"state": self.state, **metadata}


class CollectorSetupRequired(CollectorStateError):
    state = "setup_required"


class CollectorNoData(CollectorStateError):
    state = "no_data"


class Collector(Protocol):
    source_id: str

    def collect(
        self, config: dict, correlation_id: str
    ) -> "CollectionResult | list[dict]":
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
