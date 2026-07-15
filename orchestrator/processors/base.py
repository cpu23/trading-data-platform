from typing import Protocol

from budgets import BudgetContext


class Processor(Protocol):
    processor_id: str

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
