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

    def test_processor_redacts_serialized_credentials_and_url_userinfo(self):
        sentinels = (
            "json-secret-201", "json-spaced secret-202", "repr-secret-203",
            "equals-secret-204", "userinfo-secret-205", "query-secret-206",
            "encoded-user-secret-207",
        )
        message = (
            'prefix {"token":"json-secret-201"} '
            '{"password": "json-spaced secret-202"} '
            "middle {'api_key': 'repr-secret-203'} "
            "client_secret=equals-secret-204 suffix "
            "https://alice:userinfo-secret-205@example.test/path "
            "https://bob:userinfo-secret-205@example.test:8443/path?token=query-secret-206#frag "
            "https://alice%40corp:encoded-user-secret-207@example.test/encoded "
            "token_count=12 monkey=banana keyboard=qwerty"
        )

        sanitized = logging_config.redact_credentials(None, "error", {"event": message})
        rendered = json.dumps(sanitized)
        event = str(sanitized["event"])

        self.assertIn('{"token":"[REDACTED]"}', event)
        self.assertIn('{"password": "[REDACTED]"}', event)
        self.assertIn("{'api_key': '[REDACTED]'}", event)
        self.assertIn("client_secret=[REDACTED]", event)
        self.assertIn("https://[REDACTED]@example.test/path", event)
        self.assertIn("https://[REDACTED]@example.test:8443/path?token=[REDACTED]#frag", event)
        self.assertIn("https://[REDACTED]@example.test/encoded", event)
        self.assertIn("token_count=12 monkey=banana keyboard=qwerty", event)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, rendered)

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
