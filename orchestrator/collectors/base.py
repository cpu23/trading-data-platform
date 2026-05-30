from typing import Protocol


class Collector(Protocol):
    source_id: str

    def collect(self, config: dict, correlation_id: str) -> list[dict]:
        """Fetch and normalise data. Returns list of dicts matching target table schema."""
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
