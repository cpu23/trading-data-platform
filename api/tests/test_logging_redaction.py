import contextlib
import io
import json
import logging
import sys
import unittest

import structlog

import logging_config


class ApiLoggingRedactionTests(unittest.TestCase):
    def tearDown(self):
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()

    def test_api_processor_matches_recursive_and_string_redaction_contract(self):
        original = {
            "headers": {"authorization": "Bearer api-header-secret"},
            "nested": [{"Refresh_Token": "api-refresh-secret", "token_count": 3}],
            "event": "GET https://api.test/items?KEY=api-query-secret&limit=10 Bearer api-bearer-secret",
            "monkey": "safe",
        }

        sanitized = logging_config.redact_credentials(None, "info", original)
        rendered = json.dumps(sanitized)

        self.assertEqual(original["headers"]["authorization"], "Bearer api-header-secret")
        self.assertEqual(sanitized["headers"]["authorization"], "[REDACTED]")
        self.assertEqual(sanitized["nested"][0]["Refresh_Token"], "[REDACTED]")
        self.assertEqual(sanitized["nested"][0]["token_count"], 3)
        self.assertEqual(sanitized["monkey"], "safe")
        self.assertIn("limit=10", rendered)
        self.assertIn("[REDACTED]", rendered)
        for secret in ("api-header-secret", "api-refresh-secret", "api-query-secret", "api-bearer-secret"):
            self.assertNotIn(secret, rendered)

    def test_api_rendered_output_redacts_credentials_and_quiets_dependencies(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            logging_config.setup_logging("INFO")
            handler = logging.getLogger().handlers[0]
            self.assertIsInstance(handler, logging.StreamHandler)
            self.assertIs(handler.stream, sys.stdout)
            structlog.get_logger().info(
                "upstream https://api.test/data?access_token=api-access-secret&format=json",
                cookie="api-cookie-secret",
                correlation_id="api-corr-21",
            )

        output = stdout.getvalue()
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["correlation_id"], "api-corr-21")
        self.assertEqual(payload["cookie"], "[REDACTED]")
        self.assertIn("format=json", output)
        self.assertNotIn("api-access-secret", output)
        self.assertNotIn("api-cookie-secret", output)
        self.assertEqual(len(logging.getLogger().handlers), 1)
        for name in ("httpx", "httpcore", "sqlalchemy", "sqlalchemy.engine"):
            self.assertEqual(logging.getLogger(name).level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
