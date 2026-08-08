import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DASHBOARD_USER", "test")
os.environ.setdefault("DASHBOARD_PASSWORD", "test")

CONFIG = {
    "event_pipeline": {
        "sse": {
            "enabled": True,
            "heartbeat_seconds": 15,
            "poll_seconds": 0.01,
            "replay_limit": 100,
            "max_streams_per_client": 3,
        }
    }
}

import auth

with patch("config.load_config", return_value=CONFIG):
    import main

from routes import stream as stream_module


class _Request:
    def __init__(self, *, disconnected=(False,), cursor=None, config=CONFIG):
        self._disconnected = iter(disconnected)
        self.headers = {} if cursor is None else {"last-event-id": str(cursor)}
        self.client = SimpleNamespace(host="test-client")
        self.app = SimpleNamespace(state=SimpleNamespace(config=config))

    async def is_disconnected(self):
        return next(self._disconnected, True)


class SseStreamTests(unittest.TestCase):
    def tearDown(self):
        stream_module._STREAM_COUNTS.clear()

    def test_unauthenticated_stream_keeps_json_401(self):
        from fastapi.testclient import TestClient

        with (
            patch.object(auth, "setup_complete", return_value=True),
            patch.object(main, "load_config", return_value=CONFIG),
        ):
            isolated_app = main.create_app()
            with TestClient(isolated_app) as client:
                response = client.get("/stream")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["content-type"], "application/json")

    def test_last_event_id_is_strict(self):
        for value in ("", "-1", "+1", " 1", "1 ", "1.0"):
            with self.subTest(value=value):
                with self.assertRaises(stream_module.HTTPException) as raised:
                    stream_module.parse_last_event_id(value)
                self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(stream_module.parse_last_event_id("0"), 0)
        self.assertIsNone(stream_module.parse_last_event_id(None))

    def test_malformed_last_event_id_is_rejected_by_route(self):
        request = _Request()
        request.headers["last-event-id"] = "not-an-id"
        with self.assertRaises(stream_module.HTTPException) as raised:
            asyncio.run(stream_module.stream(request))
        self.assertEqual(raised.exception.status_code, 400)

    def test_replay_header_and_frame_shape(self):
        request = _Request(disconnected=(False, True), cursor=4)
        rows = [
            {
                "id": 5,
                "event_name": "watchlist_changed",
                "section_key": "watchlist",
                "scope_key": "global",
                "section_version": 7,
            },
        ]
        with (
            patch.object(stream_module, "_event_stats", return_value=(5, 5)),
            patch.object(stream_module, "_event_rows", return_value=rows) as read,
        ):
            response = asyncio.run(stream_module.stream(request))
            frame = asyncio.run(response.body_iterator.__anext__())
        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.headers["cache-control"], "no-cache, no-store")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertIn("id: 5\nevent: watchlist_changed\n", frame)
        self.assertIn(
            'data: {"section_key":"watchlist","scope_key":"global","version":7}',
            frame,
        )
        self.assertEqual(read.call_args.args[0], 4)
        self.assertEqual(read.call_args.args[1], 101)

    def test_gap_emits_one_resync_invalidation(self):
        request = _Request(disconnected=(False, True), cursor=1)
        with (
            patch.object(stream_module, "_event_stats", return_value=(10, 11)),
            patch.object(stream_module, "_event_rows", return_value=[]),
        ):
            generator = stream_module.stream_ui_events(
                request, config=CONFIG, cursor=1, host="test-client"
            )
            frame = asyncio.run(generator.__anext__())
            with self.assertRaises(StopAsyncIteration):
                asyncio.run(generator.__anext__())
        self.assertEqual(frame, "id: 11\nevent: resync_required\ndata: {}\n\n")

    def test_coalescing_keeps_latest_row_and_sorts(self):
        rows = [
            {
                "id": 2,
                "event_name": "section_changed",
                "section_key": "news",
                "scope_key": "global",
                "section_version": 2,
            },
            {
                "id": 1,
                "event_name": "watchlist_changed",
                "section_key": "watchlist",
                "scope_key": "global",
                "section_version": 1,
            },
            {
                "id": 3,
                "event_name": "section_changed",
                "section_key": "news",
                "scope_key": "global",
                "section_version": 4,
            },
        ]
        events = stream_module.coalesce_ui_events(rows)
        self.assertEqual([event["id"] for event in events], [1, 3])
        self.assertEqual(events[-1]["section_version"], 4)

    def test_disabled_stream_is_not_available(self):
        request = _Request(config={"event_pipeline": {"sse": {"enabled": False}}})
        with self.assertRaises(stream_module.HTTPException) as raised:
            asyncio.run(stream_module.stream(request))
        self.assertEqual(raised.exception.status_code, 404)

    def test_stream_limit_is_per_client(self):
        self.assertTrue(stream_module._claim_stream("same", 3))
        self.assertTrue(stream_module._claim_stream("same", 3))
        self.assertTrue(stream_module._claim_stream("same", 3))
        self.assertFalse(stream_module._claim_stream("same", 3))
        self.assertTrue(stream_module._claim_stream("other", 3))


if __name__ == "__main__":
    unittest.main()
