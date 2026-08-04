import unittest

from sqlalchemy.exc import SQLAlchemyError

from errors import (
    BudgetDenied,
    InvalidSourceData,
    PersistenceError,
    TransientSourceError,
    classify_error,
)


class ErrorTaxonomyTests(unittest.TestCase):
    def test_named_errors_have_explicit_status_class_and_retry_policy(self):
        cases = (
            (TransientSourceError("timeout"), ("failed", "transient_source", True)),
            (
                InvalidSourceData("malformed"),
                ("validation_failed", "invalid_source_data", False),
            ),
            (PersistenceError("write failed"), ("failed", "persistence", True)),
            (BudgetDenied("cap reached"), ("budget_blocked", "budget_denied", False)),
        )

        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(classify_error(error), expected)

    def test_database_boundary_failures_are_retryable_persistence_errors(self):
        self.assertEqual(
            classify_error(SQLAlchemyError("database unavailable")),
            ("failed", "persistence", True),
        )

    def test_unknown_failures_do_not_inherit_retryability(self):
        self.assertEqual(
            classify_error(RuntimeError("unexpected")),
            ("failed", "unknown", False),
        )

    def test_compatibility_facade_exports_lifecycle_conflicts_and_taxonomy(self):
        import orchestrator
        from run_lifecycle import RunAcceptanceConflict, RunStartConflict

        self.assertIs(orchestrator.RunAcceptanceConflict, RunAcceptanceConflict)
        self.assertIs(orchestrator.RunStartConflict, RunStartConflict)
        self.assertIs(orchestrator.TransientSourceError, TransientSourceError)


if __name__ == "__main__":
    unittest.main()
