import contextlib
import io
import json
import logging
import unittest

import structlog

import logging_config


SENTINELS = {
    "dict-secret-91",
    "nested-secret-92",
    "query-secret-93",
    "bearer-secret-94",
    "basic-secret-95",
    "header-secret-96",
}


class BrokenRepr:
    def __repr__(self):
        raise RuntimeError("repr failed")


class LoggingRedactionTests(unittest.TestCase):
    def tearDown(self):
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()

    def test_processor_recursively_redacts_exact_sensitive_keys_without_mutation(self):
        original = {
            "API_KEY": "dict-secret-91",
            "headers": {"Authorization": "Bearer header-secret-96", "X-Trace": "keep"},
            "items": [
                {"client-secret": "nested-secret-92", "token_count": 17},
                ("safe", {"Set-Cookie": "session=nested-secret-92"}),
            ],
            "monkey": "banana",
            "keyboard": "qwerty",
        }

        sanitized = logging_config.redact_credentials(None, "info", original)

        self.assertIsNot(sanitized, original)
        self.assertEqual(original["API_KEY"], "dict-secret-91")
        self.assertEqual(original["headers"]["Authorization"], "Bearer header-secret-96")
        self.assertEqual(sanitized["API_KEY"], "[REDACTED]")
        self.assertEqual(sanitized["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(sanitized["headers"]["X-Trace"], "keep")
        self.assertEqual(sanitized["items"][0]["client-secret"], "[REDACTED]")
        self.assertEqual(sanitized["items"][0]["token_count"], 17)
        self.assertEqual(sanitized["items"][1][1]["Set-Cookie"], "[REDACTED]")
        self.assertEqual(sanitized["monkey"], "banana")
        self.assertEqual(sanitized["keyboard"], "qwerty")

    def test_processor_sanitizes_every_explicit_credential_key_case_insensitively(self):
        credential_keys = (
            "api_key", "APIKEY", "Token", "ACCESS_TOKEN", "refresh-token", "key",
            "Authorization", "PROXY_AUTHORIZATION", "password", "PASSWD", "secret",
            "CLIENT_SECRET", "Cookie", "set-cookie",
        )
        event = {name: f"sentinel-{index}" for index, name in enumerate(credential_keys)}

        sanitized = logging_config.redact_credentials(None, "info", event)

        self.assertEqual(set(sanitized), set(event))
        self.assertTrue(all(value == "[REDACTED]" for value in sanitized.values()))
        self.assertFalse(any(value in json.dumps(sanitized) for value in event.values()))

    def test_processor_sanitizes_embedded_urls_and_free_form_credentials(self):
        message = (
            "failed https://example.test/v1?q=keep&ApiKey=query-secret-93#frag "
            "Authorization: Bearer bearer-secret-94 and Basic basic-secret-95"
        )

        sanitized = logging_config.redact_credentials(None, "error", {"event": message})
        rendered = json.dumps(sanitized)

        self.assertIn("q=keep", rendered)
        self.assertIn("#frag", rendered)
        self.assertIn("[REDACTED]", rendered)
        for secret in SENTINELS:
            self.assertNotIn(secret, rendered)

    def test_processor_handles_custom_values_without_throwing(self):
        sanitized = logging_config.redact_credentials(
            None, "info", {"event": "custom", "value": BrokenRepr()}
        )
        rendered = json.dumps(sanitized)
        self.assertIn("custom", rendered)
        self.assertIn("unserializable", rendered)

    def test_rendered_structlog_output_is_redacted_and_keeps_structured_fields(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            logging_config.setup_logging("INFO", correlation_id="corr-21")
            structlog.get_logger().info(
                "request https://example.test/path?token=query-secret-93&view=full",
                headers={"Authorization": "Bearer header-secret-96"},
                nested={"password": "nested-secret-92"},
            )

        output = stderr.getvalue()
        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["correlation_id"], "corr-21")
        self.assertEqual(payload["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(payload["nested"]["password"], "[REDACTED]")
        self.assertIn("[REDACTED]", output)
        for secret in SENTINELS:
            self.assertNotIn(secret, output)

    def test_setup_uses_stdout_only_and_quiets_dependencies_unless_debug(self):
        logging_config.setup_logging("INFO")
        self.assertEqual(len(logging.getLogger().handlers), 1)
        for name in ("httpx", "httpcore", "sqlalchemy", "sqlalchemy.engine"):
            self.assertEqual(logging.getLogger(name).level, logging.WARNING)

        logging_config.setup_logging("DEBUG")
        for name in ("httpx", "httpcore", "sqlalchemy", "sqlalchemy.engine"):
            self.assertEqual(logging.getLogger(name).level, logging.DEBUG)


if __name__ == "__main__":
    unittest.main()
