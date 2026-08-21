import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events.canonicalize import (
    build_market_event,
    canonical_json,
    content_hash,
    dedupe_key,
)
from events.contracts import EntityRef, MarketEvent, MarketEventType, MarketRef


class CanonicalizationTests(unittest.TestCase):
    def test_mapping_order_does_not_change_canonical_json_or_hash(self):
        left = {"series": "GDP", "value": 3.1, "meta": {"b": 2, "a": 1}}
        right = {"meta": {"a": 1, "b": 2}, "value": 3.1, "series": "GDP"}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(content_hash(left), content_hash(right))

    def test_changed_source_content_changes_hash(self):
        self.assertNotEqual(
            content_hash({"series": "GDP", "value": 3.1}),
            content_hash({"series": "GDP", "value": 3.2}),
        )

    def test_stable_source_id_produces_stable_dedupe_key(self):
        self.assertEqual(
            dedupe_key("fred", "GDP:2026-01-01"),
            dedupe_key("fred", "GDP:2026-01-01"),
        )
        self.assertNotEqual(
            dedupe_key("fred", "GDP:2026-01-01"),
            dedupe_key("fred", "GDP:2026-04-01"),
        )

    def test_identity_fields_required_without_source_id(self):
        with self.assertRaises(ValueError):
            dedupe_key("manual", event_type="manual_research_event")

    def test_naive_datetime_and_non_finite_numbers_are_rejected(self):
        with self.assertRaises(TypeError):
            canonical_json({"observed_at": datetime(2026, 1, 1)})
        with self.assertRaises(TypeError):
            canonical_json({"value": float("nan")})


class MarketEventContractTests(unittest.TestCase):
    def setUp(self):
        self.observed_at = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
        self.entity = EntityRef(
            entity_type="instrument",
            canonical_id="fred:GDP",
            display_name="US GDP",
            confidence=1.0,
            mapping_source="source",
        )

    def _build(self, **overrides):
        values = {
            "event_type": MarketEventType.MACRO_RELEASE,
            "source": "fred",
            "source_event_id": "GDP:2026-04-01T00:00:00+00:00",
            "observed_at": self.observed_at,
            "effective_at": self.observed_at,
            "published_at": self.observed_at + timedelta(days=30),
            "payload": {"series_id": "GDP", "value": 3.1},
            "entities": [self.entity],
            "markets": [],
            "horizons": ["medium"],
            "importance_hint": 0.8,
            "metadata": {"frequency": "quarterly"},
            "correlation_id": uuid4(),
        }
        values.update(overrides)
        return build_market_event(**values)

    def test_builder_returns_complete_strict_envelope(self):
        event = self._build()
        payload = event.model_dump(mode="json")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["event_type"], "macro_release")
        self.assertEqual(payload["source"], "fred")
        self.assertEqual(payload["dedupe_key"], f"fred:{event.source_event_id}")
        self.assertEqual(len(payload["content_hash"]), 64)
        self.assertEqual(payload["observed_at"], "2026-08-05T12:00:00Z")
        self.assertIsNone(payload["revision_of_event_id"])

    def test_revision_link_does_not_manufacture_content_change(self):
        release = self._build(event_type="macro_release")
        revision = self._build(
            event_type="macro_revision",
            revision_of_event_id=release.event_id,
        )
        self.assertEqual(release.content_hash, revision.content_hash)
        self.assertEqual(revision.revision_of_event_id, release.event_id)

    def test_payload_change_creates_new_content_hash(self):
        before = self._build(payload={"series_id": "GDP", "value": 3.1})
        after = self._build(payload={"series_id": "GDP", "value": 3.2})
        self.assertNotEqual(before.content_hash, after.content_hash)

    def test_naive_observed_at_is_rejected(self):
        with self.assertRaises(ValueError):
            self._build(observed_at=datetime(2026, 8, 5, 12, 0))

    def test_invalid_confidence_and_importance_are_rejected(self):
        with self.assertRaises(ValidationError):
            EntityRef(
                entity_type="instrument",
                canonical_id="fred:GDP",
                display_name="GDP",
                confidence=1.1,
                mapping_source="source",
            )
        with self.assertRaises(ValidationError):
            self._build(importance_hint=-0.01)

    def test_unknown_fields_and_non_json_payload_are_rejected(self):
        event = self._build()
        data = event.model_dump()
        data["unexpected"] = True
        with self.assertRaises(ValidationError):
            MarketEvent.model_validate(data)
        with self.assertRaises(TypeError):
            self._build(payload={"bad": {"not", "json"}})

    def test_nullable_envelope_fields_are_required(self):
        data = self._build().model_dump()
        del data["source_payload_id"]
        with self.assertRaises(ValidationError):
            MarketEvent.model_validate(data)


class FreeSourceEventTypeTests(unittest.TestCase):
    def setUp(self):
        self.observed_at = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def test_new_free_source_event_types_are_explicit(self):
        self.assertEqual(MarketEventType.TRANSCRIPT_PUBLISHED.value, "transcript_published")
        self.assertEqual(
            MarketEventType.OPTION_CHAIN_PUBLISHED.value, "option_chain_published"
        )
        self.assertEqual(
            MarketEventType.CORPORATE_ACTION_PUBLISHED.value,
            "corporate_action_published",
        )

    def test_compact_option_snapshot_event_has_stable_identity(self):
        captured = self.observed_at
        payload = {
            "symbol": "AAPL",
            "captured_at": captured.isoformat(),
            "contract_count": 2,
            "contracts_by_type": {"call": 1, "put": 1},
            "expiration_count": 1,
            "source_timestamp_min": "2026-08-05T11:59:00+00:00",
            "source_timestamp_max": "2026-08-05T12:00:00+00:00",
            "expiration_min": "2026-09-18",
            "expiration_max": "2026-09-18",
            "underlying_price": 232.0,
            "delayed": True,
            "delay_minutes": 15,
            "truncated": {
                "symbols": False,
                "expiries": False,
                "contracts": False,
            },
        }
        markets = [
            MarketRef(
                canonical_id="equity:AAPL",
                display_name="AAPL",
                asset_class="equity",
                symbol="AAPL",
            )
        ]
        event = build_market_event(
            MarketEventType.OPTION_CHAIN_PUBLISHED,
            "cboe_options",
            captured,
            payload,
            source_event_id=f"AAPL:{captured.isoformat()}",
            effective_at=captured,
            markets=markets,
            identity={"symbol": "AAPL", "captured_at": captured.isoformat()},
        )
        self.assertEqual(event.event_type, "option_chain_published")
        self.assertEqual(
            event.dedupe_key, f"cboe_options:AAPL:{captured.isoformat()}"
        )
        self.assertEqual(event.source_event_id, f"AAPL:{captured.isoformat()}")
        self.assertEqual(event.markets[0].canonical_id, "equity:AAPL")
        # Identical snapshots hash identically; changed quotes do not.
        replay = build_market_event(
            MarketEventType.OPTION_CHAIN_PUBLISHED,
            "cboe_options",
            captured,
            payload,
            source_event_id=f"AAPL:{captured.isoformat()}",
            effective_at=captured,
            markets=markets,
            identity={"symbol": "AAPL", "captured_at": captured.isoformat()},
        )
        self.assertEqual(event.content_hash, replay.content_hash)
        self.assertEqual(
            dedupe_key("cboe_options", f"AAPL:{captured.isoformat()}"),
            event.dedupe_key,
        )


if __name__ == "__main__":
    unittest.main()
