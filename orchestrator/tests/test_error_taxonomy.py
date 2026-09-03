import unittest

from sqlalchemy.exc import SQLAlchemyError

from errors import (
    BudgetDenied,
    InvalidSourceData,
    PersistenceError,
    TransientSourceError,
    classify_error,
    sanitize_error,
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


    def test_sanitize_error_none_returns_none(self):
        self.assertIsNone(sanitize_error(None))

    def test_sanitize_error_exceptions_return_safe_type_name(self):
        self.assertEqual(sanitize_error(RuntimeError("token=secret")), "RuntimeError")
        self.assertEqual(sanitize_error(ValueError("database password=123")), "ValueError")

    def test_sanitize_error_normalizes_whitespace_and_control_characters(self):
        self.assertEqual(
            sanitize_error("  bad   request  \n\t  with newline \r\n and tabs "),
            "bad request with newline and tabs",
        )

    def test_sanitize_error_redacts_credentials_and_secrets(self):
        self.assertEqual(
            sanitize_error("failed with api_key=secret123 and password: mypass"),
            "failed with api_key=[REDACTED] and password: [REDACTED]",
        )
        self.assertEqual(
            sanitize_error("Authorization: Bearer super-secret-token-val"),
            "Authorization: Bearer [REDACTED]",
        )
        self.assertEqual(
            sanitize_error("connect https://admin:pass123@api.example.com/v1"),
            "connect https://[REDACTED]@api.example.com/v1",
        )

    def test_sanitize_error_enforces_maximum_length(self):
        long_error = "a" * 1000
        sanitized = sanitize_error(long_error)
        self.assertIsNotNone(sanitized)
        self.assertEqual(len(sanitized), 500)

    def test_sanitize_error_empty_string_falls_back_to_error(self):
        self.assertEqual(sanitize_error(""), "error")
        self.assertEqual(sanitize_error("   \n\t   "), "error")

if __name__ == "__main__":
    unittest.main()
