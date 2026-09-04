import os
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo_live import DEMO_PRICES, publish_demo_tick


class DemoLivePublisherTests(unittest.TestCase):
    def test_tick_publishes_bounded_prices(self):
        session = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = session
        context.__exit__.return_value = False
        observed_at = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        with (
            patch.dict(os.environ, {"DEMO_MODE": "true"}, clear=False),
            patch("demo_live.get_session", return_value=context),
        ):
            result = publish_demo_tick({"demo": {"enabled": True}}, 3, now=observed_at)

        self.assertEqual(session.execute.call_count, len(DEMO_PRICES))
        self.assertEqual(result["price_rows"], len(DEMO_PRICES))
        self.assertIsNone(result["event_id"])
        session.commit.assert_called_once_with()

    def test_publisher_refuses_to_run_outside_demo_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DEMO_MODE=true"):
                publish_demo_tick({"demo": {"enabled": True}}, 0)


if __name__ == "__main__":
    unittest.main()
