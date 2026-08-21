"""Tests for catalyst event playbooks (migration 051 / thesis_playbooks).

The pure builder and matcher are exercised directly; repository helpers use
a queued-result fake session (mirroring test_thesis_fusion.py) so SQL text,
bound parameters, and the no-commit contract are asserted.  SQL guard
behavior and migration idempotency are covered in test_migrations.py.
"""

import os
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

os.environ.update(
    {
        "STATE_DIR": "/tmp/trading-data-thesis-playbooks-test-state",
        "DASHBOARD_USER": "test",
        "DASHBOARD_PASSWORD": "test",
        "DEPLOYMENT_MODE": "test",
        "LEGACY_BASIC_AUTH": "1",
        "CONFIG_DIR": str(ORCH_ROOT.parent / "config"),
        "DB_USER": "test",
        "DB_PASSWORD": "test",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_NAME": "trading_data",
        "FRED_API_KEY": "test",
        "OPENROUTER_API_KEY": "test",
        "OPENROUTER_MODEL": "test/model",
        "OANDA_API_KEY": "test",
        "SECRETS_FILE": "/nonexistent/test-secrets.env",
    }
)

from research_intelligence.contracts import (  # noqa: E402
    NormalizedEvidence,
)
from thesis_playbooks import (  # noqa: E402
    PlaybookDraft,
    build_event_playbook,
    event_matches_playbook,
    list_due_playbooks,
    list_playbook_history,
    record_event_match,
    upsert_event_playbook,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
THESIS_ID = "22222222-2222-4222-8222-222222222222"
PLAYBOOK_ID = UUID("89898989-8989-4989-8989-898989898989")
MARKET_EVENT_ID = UUID("99999999-9999-4999-8999-999999999999")


class Result:
    def __init__(self, *, first=None, rows=None):
        self._first = first
        self._rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._rows


class Session:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.commit = MagicMock()

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        if not self.results:
            raise AssertionError(f"unexpected SQL call: {statement}")
        return self.results.pop(0)


def evidence_item(
    evidence_type="official_document",
    evidence_id="sec:10q:nvda:2026q2",
    source_name="SEC EDGAR",
    **overrides,
):
    value = {
        "evidence_type": evidence_type,
        "evidence_id": evidence_id,
        "source_name": source_name,
        "source_timestamp": NOW,
        "available_at": NOW,
        "title": "Sample evidence",
        "point_in_time_safe": True,
    }
    value.update(overrides)
    return NormalizedEvidence.create(**value)


def candidate(**overrides):
    value = {
        "catalyst": "Nvidia Q2 earnings release confirms capex guide raise",
        "horizon": "weeks",
        "subject": "Nvidia Corp",
        "instrument": "NVDA",
        "scenarios": [
            {"label": "bull", "probability": 0.3, "expected_return": 0.25},
            {"label": "base", "probability": 0.5, "expected_return": 0.05},
            {"label": "bear", "probability": 0.2, "expected_return": -0.30},
        ],
        "invalidators": ["Capex guide cut", "Data center revenue miss"],
        "missing_evidence": ["Management Q3 guidance", "Peer capex confirmation"],
        "evidence_refs": ["official_document:sec:10q:nvda:2026q2"],
    }
    value.update(overrides)
    return value


def build_draft(**overrides):
    supplied_evidence = overrides.pop("evidence", [evidence_item()])
    candidate_overrides = dict(overrides.pop("candidate", {}))
    if "evidence_refs" not in candidate_overrides:
        candidate_overrides["evidence_refs"] = [item.ref for item in supplied_evidence]
    return build_event_playbook(
        candidate(**candidate_overrides),
        supplied_evidence,
        thesis_id=overrides.pop("thesis_id", THESIS_ID),
        as_of=overrides.pop("as_of", NOW),
        **overrides,
    )


class PlaybookBuildTests(unittest.TestCase):
    def test_inference_is_deterministic_and_fingerprint_covers_content(self):
        draft = build_draft()
        again = build_draft()
        self.assertEqual(draft, again)
        self.assertEqual(draft.input_fingerprint, again.input_fingerprint)
        # Event types are inferred from the cited evidence families only.
        self.assertEqual(
            draft.event_types,
            ("filing_ingested", "regulatory_filing_published"),
        )
        # The fingerprint covers all persisted content: changing any content
        # field changes it, and the build time never does.
        changed = build_draft(candidate={"catalyst": "A different catalyst"})
        self.assertNotEqual(changed.input_fingerprint, draft.input_fingerprint)
        changed = build_draft(candidate={"horizon": "months"})
        self.assertNotEqual(changed.input_fingerprint, draft.input_fingerprint)
        changed = build_draft(candidate={"evidence_refs": []}, evidence=[])
        self.assertNotEqual(changed.input_fingerprint, draft.input_fingerprint)
        self.assertEqual(
            build_draft(as_of=datetime(2026, 8, 1, 0, 0, tzinfo=UTC)).input_fingerprint,
            draft.input_fingerprint,
        )

    def test_company_expectations_map_to_calendar_research_events(self):
        expectations = evidence_item(
            source_name="Apple",
            provenance={"source": "company_expectations"},
        )
        draft = build_draft(evidence=[expectations])
        self.assertEqual(
            draft.event_types,
            ("calendar_event_changed", "manual_research_event"),
        )

    def test_playbook_key_groups_thesis_catalyst_horizon(self):
        draft = build_draft()
        self.assertEqual(draft.playbook_key, build_draft().playbook_key)
        self.assertNotEqual(
            draft.playbook_key,
            build_draft(candidate={"catalyst": "A different catalyst"}).playbook_key,
        )
        self.assertNotEqual(
            draft.playbook_key,
            build_draft(thesis_id="23232323-2323-4232-8232-232323232323").playbook_key,
        )
        self.assertNotEqual(
            draft.playbook_key,
            build_draft(candidate={"horizon": "months"}).playbook_key,
        )

    def test_opposing_scenario_legs_preserved_verbatim(self):
        draft = build_draft()
        self.assertEqual(
            draft.bull_scenario,
            {"label": "bull", "probability": 0.3, "expected_return": 0.25},
        )
        self.assertEqual(
            draft.base_scenario,
            {"label": "base", "probability": 0.5, "expected_return": 0.05},
        )
        self.assertEqual(
            draft.bear_scenario,
            {"label": "bear", "probability": 0.2, "expected_return": -0.30},
        )
        # Unknown legs stay None instead of being fabricated.
        partial = build_draft(candidate={"scenarios": []})
        self.assertIsNone(partial.bull_scenario)
        self.assertIsNone(partial.base_scenario)
        self.assertIsNone(partial.bear_scenario)

    def test_conditions_derive_verbatim_from_candidate_fields(self):
        draft = build_draft()
        self.assertEqual(
            draft.trigger_conditions,
            ("Nvidia Q2 earnings release confirms capex guide raise",),
        )
        self.assertEqual(
            draft.invalidation_conditions,
            ("Capex guide cut", "Data center revenue miss"),
        )
        self.assertEqual(
            draft.confirmation_conditions,
            ("Management Q3 guidance", "Peer capex confirmation"),
        )
        # The catalyst is the trigger: no new text is invented.
        self.assertEqual(draft.trigger_conditions, (draft.catalyst,))

    def test_evidence_families_map_to_bounded_event_types(self):
        cases = [
            (
                "official_document",
                "SEC EDGAR",
                ("filing_ingested", "regulatory_filing_published"),
            ),
            (
                "filing_delta",
                "SEC EDGAR",
                ("filing_ingested", "regulatory_filing_published"),
            ),
            (
                "macro_release",
                "BLS CPI",
                (
                    "calendar_event_changed",
                    "central_bank_communication",
                    "macro_release",
                    "macro_revision",
                ),
            ),
            (
                "market_state",
                "NASDAQ daily",
                (
                    "corporate_action_published",
                    "correlation_state_changed",
                    "price_bar_closed",
                    "price_tick",
                    "volatility_state_changed",
                ),
            ),
            ("investment_analysis", "Desk research", ("manual_research_event",)),
        ]
        for evidence_type, source, expected in cases:
            draft = build_draft(evidence=[evidence_item(evidence_type, "id-1", source)])
            self.assertEqual(draft.event_types, expected)

    def test_options_and_positioning_inferred_from_source_keywords(self):
        draft = build_draft(
            evidence=[
                evidence_item(
                    "source_claim", "claim:cftc", "CFTC Commitment of Traders"
                )
            ]
        )
        self.assertIn("positioning_report_published", draft.event_types)
        self.assertIn("headline_published", draft.event_types)
        draft = build_draft(
            evidence=[
                evidence_item(
                    "source_claim", "claim:transcript", "Earnings Call Transcript"
                )
            ]
        )
        self.assertIn("transcript_published", draft.event_types)
        draft = build_draft(
            evidence=[evidence_item("market_state", "chain:nvda", "CBOE Options Chain")]
        )
        self.assertIn("option_chain_published", draft.event_types)

    def test_unsupported_evidence_never_invents_event_types(self):
        # A cited evidence item whose type has no family mapping (and no
        # options/positioning source keywords) contributes no event types:
        # the builder never invents events outside the vocabulary.
        unmapped = NormalizedEvidence(
            evidence_type="unknown_kind",
            evidence_id="x-1",
            source_name="Mystery source",
            source_timestamp=NOW,
            available_at=NOW,
            availability_basis="observed",
            acquired_at=None,
            valid_from=None,
            valid_to=None,
            point_in_time_safe=True,
            title="t",
            bounded_excerpt=None,
            source_reference=None,
            entities=(),
            structured_fields={},
            provenance={},
            freshness="current",
            content_fingerprint="0" * 64,
        )
        draft = build_draft(
            evidence=[unmapped],
            candidate={"evidence_refs": ["unknown_kind:x-1"]},
        )
        self.assertEqual(draft.event_types, ())

    def test_event_types_never_leave_the_market_event_vocabulary(self):
        from events.contracts import MarketEventType

        vocabulary = {item.value for item in MarketEventType}
        items = [
            evidence_item("official_document", "id-a", "SEC EDGAR"),
            evidence_item("macro_release", "id-b", "BLS CPI"),
            evidence_item("source_claim", "id-c", "CFTC Commitment of Traders"),
            evidence_item("market_state", "id-d", "NASDAQ daily"),
        ]
        draft = build_draft(
            evidence=items, candidate={"evidence_refs": [i.ref for i in items]}
        )
        self.assertTrue(set(draft.event_types) <= vocabulary)
        self.assertLessEqual(len(draft.event_types), 18)

    def test_unknown_evidence_ref_raises(self):
        with self.assertRaisesRegex(ValueError, "unknown evidence id"):
            build_draft(candidate={"evidence_refs": ["official_document:missing"]})

    def test_cited_evidence_refs_preserved_exactly(self):
        items = [
            evidence_item("official_document", "sec:10q:nvda:2026q2", "SEC EDGAR"),
            evidence_item("macro_release", "cpi:2026-07", "BLS CPI"),
        ]
        refs = tuple(item.ref for item in items)
        draft = build_draft(evidence=items, candidate={"evidence_refs": list(refs)})
        self.assertEqual(draft.cited_evidence_refs, refs)

    def test_bounds_and_numerics_rejected(self):
        with self.assertRaisesRegex(ValueError, "too many items"):
            build_draft(
                candidate={"evidence_refs": [f"source_claim:r{i}" for i in range(31)]},
                evidence=[
                    evidence_item("source_claim", f"r{i}", "News") for i in range(31)
                ],
            )
        with self.assertRaisesRegex(ValueError, "too many items"):
            build_draft(candidate={"invalidators": [f"i{i}" for i in range(21)]})
        with self.assertRaisesRegex(ValueError, "too many items"):
            build_draft(candidate={"missing_evidence": [f"m{i}" for i in range(21)]})
        with self.assertRaisesRegex(ValueError, "invalid probability"):
            build_draft(
                candidate={
                    "scenarios": [
                        {"label": "bull", "probability": 1.5, "expected_return": 1.0}
                    ]
                }
            )
        with self.assertRaisesRegex(ValueError, "invalid expected_return"):
            build_draft(
                candidate={
                    "scenarios": [
                        {"label": "bull", "probability": 0.5, "expected_return": 101.0}
                    ]
                }
            )
        with self.assertRaisesRegex(ValueError, "invalid expected_return"):
            build_draft(
                candidate={
                    "scenarios": [
                        {
                            "label": "bull",
                            "probability": 0.5,
                            "expected_return": float("nan"),
                        }
                    ]
                }
            )
        with self.assertRaisesRegex(ValueError, "unsupported scenario label"):
            build_draft(
                candidate={
                    "scenarios": [
                        {"label": "moon", "probability": 0.5, "expected_return": 1.0}
                    ]
                }
            )
        with self.assertRaisesRegex(ValueError, "unsupported horizon"):
            build_draft(candidate={"horizon": "decade"})
        with self.assertRaisesRegex(ValueError, "catalyst is required"):
            build_draft(candidate={"catalyst": ""})
        with self.assertRaisesRegex(ValueError, "exceeds maximum length"):
            build_draft(candidate={"catalyst": "x" * 2001})

    def test_naive_datetimes_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            build_draft(as_of=datetime(2026, 8, 15, 12, 0))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            build_draft(
                expected_at=datetime(2026, 9, 15, 12, 0),
            )

    def test_expected_at_is_optional_and_bounded_to_the_vocabulary(self):
        self.assertIsNone(build_draft().expected_at)
        expected = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
        draft = build_draft(expected_at=expected.isoformat())
        self.assertEqual(draft.expected_at, expected)

    def test_draft_accepts_a_candidate_draft_object(self):
        from thesis_tournament import CandidateDraft

        draft = CandidateDraft(
            role="fundamental",
            index=0,
            claim="Capex upcycle benefits NVDA.",
            subject="Nvidia Corp",
            instrument="NVDA",
            direction="long",
            horizon="weeks",
            consensus="Neutral",
            variant_perception="Underappreciated",
            mechanism="Capex",
            catalyst="Q2 earnings release",
            scenarios=(),
            invalidators=(),
            missing_evidence=(),
            evidence_refs=["official_document:sec:10q:nvda:2026q2"],
            confidence=0.6,
            candidate_key="k",
            content_fingerprint="a" * 64,
            completeness=1.0,
        )
        built = build_event_playbook(
            draft,
            [evidence_item()],
            thesis_id=THESIS_ID,
            as_of=NOW,
        )
        self.assertEqual(built.catalyst, "Q2 earnings release")
        self.assertEqual(built.entity_keys, ("nvidia corp", "nvda"))


class PlaybookDraftStrictTests(unittest.TestCase):
    def test_create_rejects_invalid_identifiers_and_fingerprints(self):
        with self.assertRaisesRegex(ValueError, "invalid thesis_id"):
            PlaybookDraft.create(
                thesis_id="nope",
                playbook_key="0" * 64,
                catalyst="c",
                horizon="weeks",
                input_fingerprint="0" * 64,
            )
        with self.assertRaisesRegex(ValueError, "invalid playbook_key"):
            PlaybookDraft.create(
                thesis_id=THESIS_ID,
                playbook_key="short",
                catalyst="c",
                horizon="weeks",
                input_fingerprint="0" * 64,
            )
        with self.assertRaisesRegex(ValueError, "invalid input_fingerprint"):
            PlaybookDraft.create(
                thesis_id=THESIS_ID,
                playbook_key="0" * 64,
                catalyst="c",
                horizon="weeks",
                input_fingerprint="z" * 64,
            )
        with self.assertRaisesRegex(ValueError, "invalid thesis_version"):
            PlaybookDraft.create(
                thesis_id=THESIS_ID,
                playbook_key="0" * 64,
                catalyst="c",
                horizon="weeks",
                thesis_version=0,
                input_fingerprint="0" * 64,
            )

    def test_create_rejects_unsupported_event_types(self):
        with self.assertRaisesRegex(ValueError, "unsupported event type"):
            PlaybookDraft.create(
                thesis_id=THESIS_ID,
                playbook_key="0" * 64,
                catalyst="c",
                horizon="weeks",
                event_types=["catalyst_announced"],
                input_fingerprint="0" * 64,
            )

    def test_to_dict_round_trips_persisted_content(self):
        draft = build_draft()
        payload = draft.to_dict()
        self.assertEqual(payload["thesis_id"], THESIS_ID)
        self.assertEqual(payload["catalyst"], draft.catalyst)
        self.assertEqual(payload["event_types"], list(draft.event_types))
        self.assertEqual(payload["bear_scenario"], dict(draft.bear_scenario))
        self.assertEqual(
            payload["cited_evidence_refs"], list(draft.cited_evidence_refs)
        )
        rebuilt = PlaybookDraft.create(**{key: value for key, value in payload.items()})
        self.assertEqual(rebuilt, draft)


class EventMatchTests(unittest.TestCase):
    @staticmethod
    def _event(event_type, *, company="nvidia corp", symbol="NVDA", event_id=None):
        from events.contracts import (
            EntityRef,
            MarketEvent,
            MarketEventType,
            MarketRef,
        )

        return MarketEvent(
            schema_version=1,
            event_id=event_id or uuid4(),
            event_type=MarketEventType(event_type),
            source="sec",
            source_event_id="s",
            source_payload_id=None,
            observed_at=NOW,
            effective_at=NOW,
            published_at=NOW,
            ingested_at=NOW,
            revision_of_event_id=None,
            content_hash="a" * 64,
            dedupe_key="k",
            entities=[
                EntityRef(
                    entity_type="company",
                    canonical_id=company,
                    display_name=company,
                    confidence=1.0,
                    mapping_source="source",
                )
            ],
            markets=[
                MarketRef(
                    canonical_id=symbol.casefold(),
                    display_name=symbol,
                    asset_class="equity",
                    symbol=symbol,
                )
            ],
            horizons=["medium"],
            importance_hint=None,
            payload={},
            metadata={},
            correlation_id=uuid4(),
        )

    def test_type_and_entity_overlap_match(self):
        draft = build_draft()
        match = event_matches_playbook(
            self._event("regulatory_filing_published"), draft
        )
        self.assertTrue(match.matched)
        self.assertEqual(match.event_type, "regulatory_filing_published")
        self.assertIn("nvda", match.matched_entities)
        self.assertIn("nvidia corp", match.matched_entities)
        self.assertEqual(match.playbook_key, draft.playbook_key)

    def test_type_mismatch_never_matches(self):
        draft = build_draft()
        match = event_matches_playbook(self._event("central_bank_communication"), draft)
        self.assertFalse(match.matched)
        self.assertEqual(match.event_type, "central_bank_communication")

    def test_entity_mismatch_never_matches(self):
        draft = build_draft()
        match = event_matches_playbook(
            self._event(
                "regulatory_filing_published", company="amd corp", symbol="AMD"
            ),
            draft,
        )
        self.assertFalse(match.matched)

    def test_no_entity_keys_is_conservative(self):
        draft = build_draft()
        row = draft.to_dict()
        del row["entity_keys"]
        self.assertFalse(
            event_matches_playbook(self._event("regulatory_filing_published"), row)
        )
        self.assertTrue(
            event_matches_playbook(
                self._event("regulatory_filing_published"),
                row,
                entity_keys=["NVDA"],
            ).matched
        )

    def test_unknown_event_type_never_matches(self):
        draft = build_draft()
        match = event_matches_playbook(
            {"event_type": "not_a_type", "entities": [], "markets": []}, draft
        )
        self.assertFalse(match.matched)
        self.assertIsNone(match.event_type)

    def test_mapping_shapes_are_supported(self):
        draft = build_draft()
        event = {
            "event_id": str(uuid4()),
            "event_type": "regulatory_filing_published",
            "markets": [{"symbol": "NVDA", "canonical_id": "nvda"}],
            "entities": [
                {
                    "entity_type": "company",
                    "canonical_id": "nvidia corp",
                    "display_name": "Nvidia Corp",
                }
            ],
        }
        match = event_matches_playbook(event, draft)
        self.assertTrue(match.matched)
        self.assertEqual(match.event_id, event["event_id"])


class UpsertPlaybookTests(unittest.TestCase):
    def test_insert_then_idempotent_noop(self):
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no active playbook
                Result(first={"id": PLAYBOOK_ID, "version": 1}),  # INSERT RETURNING
            ]
        )
        draft = build_draft()
        result = upsert_event_playbook(session, draft)
        self.assertEqual(
            result, {"id": str(PLAYBOOK_ID), "version": 1, "changed": True}
        )
        session.commit.assert_not_called()
        statement, params = session.calls[2]
        self.assertIn(
            "insert into investment_thesis_event_playbooks", statement.lower()
        )
        self.assertEqual(params["playbook_key"], draft.playbook_key)
        self.assertEqual(params["version"], 1)
        self.assertEqual(params["input_fingerprint"], draft.input_fingerprint)
        self.assertIn("cast(:event_types as text[])", statement.lower())
        self.assertIn("cast(:trigger_conditions as jsonb)", statement.lower())

        # Second call with the same draft: active row matches the fingerprint.
        session = Session(
            [
                Result(first={"present": 1}),
                Result(
                    first={
                        "id": str(PLAYBOOK_ID),
                        "thesis_id": THESIS_ID,
                        "version": 1,
                        "input_fingerprint": draft.input_fingerprint,
                    }
                ),
            ]
        )
        result = upsert_event_playbook(session, draft)
        self.assertEqual(
            result, {"id": str(PLAYBOOK_ID), "version": 1, "changed": False}
        )
        self.assertEqual(len(session.calls), 2)
        session.commit.assert_not_called()

    def test_changed_content_supersedes_and_versions(self):
        session = Session(
            [
                Result(first={"present": 1}),
                Result(
                    first={
                        "id": str(PLAYBOOK_ID),
                        "thesis_id": THESIS_ID,
                        "version": 1,
                        "input_fingerprint": "0" * 64,
                    }
                ),
                Result(first=None),  # supersede UPDATE (no RETURNING)
                Result(first={"id": PLAYBOOK_ID, "version": 2}),  # INSERT RETURNING
            ]
        )
        draft = build_draft()
        result = upsert_event_playbook(session, draft)
        self.assertEqual(
            result, {"id": str(PLAYBOOK_ID), "version": 2, "changed": True}
        )
        update_statement, update_params = session.calls[2]
        self.assertIn(
            "update investment_thesis_event_playbooks", update_statement.lower()
        )
        self.assertIn("superseded_at = now()", update_statement.lower())
        # The supersede is race-safe: it only transitions still-active rows.
        self.assertIn("and superseded_at is null", update_statement.lower())
        self.assertEqual(update_params["id"], str(PLAYBOOK_ID))
        insert_statement, insert_params = session.calls[3]
        self.assertEqual(insert_params["version"], 2)
        session.commit.assert_not_called()

    def test_concurrent_active_row_semantics_are_guarded(self):
        # Two writers with different content race on the same key: both see
        # the same active v1.  Each supersede UPDATE is guarded by
        # superseded_at IS NULL so only one transition wins, and version+1
        # inserts collide on the (playbook_key, version) unique constraint
        # rather than silently overwriting history.
        session = Session(
            [
                Result(first={"present": 1}),
                Result(
                    first={
                        "id": str(PLAYBOOK_ID),
                        "thesis_id": THESIS_ID,
                        "version": 1,
                        "input_fingerprint": "0" * 64,
                    }
                ),
                Result(first=None),
                Result(first={"id": PLAYBOOK_ID, "version": 2}),
            ]
        )
        upsert_event_playbook(session, build_draft())
        statements = [statement.lower() for statement, _ in session.calls]
        active_probe = statements[1]
        self.assertIn("superseded_at is null", active_probe)
        self.assertIn("limit 1", active_probe)
        update = statements[2]
        self.assertIn("update investment_thesis_event_playbooks", update)
        self.assertIn("where id = cast(:id as uuid) and superseded_at is null", update)
        insert = statements[3]
        self.assertIn("insert into investment_thesis_event_playbooks", insert)
        self.assertIn("version", insert)

    def test_key_reuse_by_another_thesis_raises(self):
        session = Session(
            [
                Result(first={"present": 1}),
                Result(
                    first={
                        "id": str(PLAYBOOK_ID),
                        "thesis_id": "23232323-2323-4232-8232-232323232323",
                        "version": 1,
                        "input_fingerprint": "0" * 64,
                    }
                ),
            ]
        )
        with self.assertRaisesRegex(ValueError, "already in use by another thesis"):
            upsert_event_playbook(session, build_draft())

    def test_unknown_thesis_raises_and_no_commit(self):
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown thesis"):
            upsert_event_playbook(session, build_draft())
        session.commit.assert_not_called()

    def test_mapping_draft_is_accepted(self):
        session = Session(
            [
                Result(first={"present": 1}),
                Result(first=None),
                Result(first={"id": PLAYBOOK_ID, "version": 1}),
            ]
        )
        payload = build_draft().to_dict()
        result = upsert_event_playbook(session, payload)
        self.assertEqual(result["changed"], True)
        self.assertEqual(session.calls[2][1]["playbook_key"], payload["playbook_key"])


class RecordEventMatchTests(unittest.TestCase):
    def _session_with_known_refs(self):
        return Session(
            [
                Result(first={"present": 1}),  # playbook exists
                Result(first={"present": 1}),  # market event exists
                Result(first=None),  # no prior match
                Result(first=None),  # INSERT (no RETURNING)
            ]
        )

    def test_record_once_then_idempotent_noop(self):
        session = self._session_with_known_refs()
        recorded = record_event_match(
            session,
            playbook_id=str(PLAYBOOK_ID),
            market_event_id=str(MARKET_EVENT_ID),
            match_kind="trigger",
            evidence_refs=["official_document:sec:10q:nvda:2026q2"],
            observed_at=NOW,
            assessment={"event_type": "regulatory_filing_published", "matched": True},
        )
        self.assertTrue(recorded)
        session.commit.assert_not_called()
        insert_statement, insert_params = session.calls[3]
        self.assertIn(
            "insert into investment_thesis_event_matches", insert_statement.lower()
        )
        self.assertIn(
            "on conflict (playbook_id, market_event_id, match_kind) do nothing",
            insert_statement.lower(),
        )
        self.assertEqual(insert_params["match_kind"], "trigger")
        self.assertEqual(insert_params["observed_at"], NOW)
        self.assertIn("cast(:evidence_refs as text[])", insert_statement.lower())
        self.assertIn("cast(:assessment as jsonb)", insert_statement.lower())

        session = Session(
            [
                Result(first={"present": 1}),
                Result(first={"present": 1}),
                Result(first={"present": 1}),  # prior match exists
            ]
        )
        recorded = record_event_match(
            session,
            playbook_id=str(PLAYBOOK_ID),
            market_event_id=str(MARKET_EVENT_ID),
            match_kind="trigger",
        )
        self.assertFalse(recorded)
        self.assertEqual(len(session.calls), 3)
        session.commit.assert_not_called()

    def test_unknown_playbook_or_event_raises(self):
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown playbook"):
            record_event_match(
                session,
                playbook_id=str(PLAYBOOK_ID),
                market_event_id=str(MARKET_EVENT_ID),
                match_kind="trigger",
            )
        session = Session(
            [
                Result(first={"present": 1}),
                Result(first=None),
            ]
        )
        with self.assertRaisesRegex(ValueError, "unknown market event"):
            record_event_match(
                session,
                playbook_id=str(PLAYBOOK_ID),
                market_event_id=str(MARKET_EVENT_ID),
                match_kind="trigger",
            )

    def test_unsupported_kind_and_bounds_rejected(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported match_kind"):
            record_event_match(
                session,
                playbook_id=str(PLAYBOOK_ID),
                market_event_id=str(MARKET_EVENT_ID),
                match_kind="execute",
            )
        with self.assertRaisesRegex(ValueError, "must be an array"):
            record_event_match(
                session,
                playbook_id=str(PLAYBOOK_ID),
                market_event_id=str(MARKET_EVENT_ID),
                match_kind="trigger",
                evidence_refs="official_document:x",
            )
        with self.assertRaisesRegex(ValueError, "too many items"):
            record_event_match(
                session,
                playbook_id=str(PLAYBOOK_ID),
                market_event_id=str(MARKET_EVENT_ID),
                match_kind="trigger",
                evidence_refs=[f"source_claim:r{i}" for i in range(31)],
            )

    def test_naive_observed_at_and_bad_assessment_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            record_event_match(
                Session([]),
                playbook_id=str(PLAYBOOK_ID),
                market_event_id=str(MARKET_EVENT_ID),
                match_kind="trigger",
                observed_at=datetime(2026, 8, 15, 12, 0),
            )
        with self.assertRaisesRegex(ValueError, "assessment must be an object"):
            record_event_match(
                Session([]),
                playbook_id=str(PLAYBOOK_ID),
                market_event_id=str(MARKET_EVENT_ID),
                match_kind="trigger",
                assessment=["trigger"],
            )
        with self.assertRaisesRegex(ValueError, "non-finite"):
            record_event_match(
                Session([]),
                playbook_id=str(PLAYBOOK_ID),
                market_event_id=str(MARKET_EVENT_ID),
                match_kind="trigger",
                assessment={"score": float("inf")},
            )
        with self.assertRaisesRegex(ValueError, "oversized string"):
            record_event_match(
                Session([]),
                playbook_id=str(PLAYBOOK_ID),
                market_event_id=str(MARKET_EVENT_ID),
                match_kind="trigger",
                assessment={"note": "x" * 1001},
            )
        with self.assertRaisesRegex(ValueError, "nests too deeply"):
            record_event_match(
                Session([]),
                playbook_id=str(PLAYBOOK_ID),
                market_event_id=str(MARKET_EVENT_ID),
                match_kind="trigger",
                assessment={"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}},
            )


class ListPlaybookTests(unittest.TestCase):
    def test_due_playbooks_are_bounded_and_stably_ordered(self):
        session = Session(
            [
                Result(
                    rows=[
                        {
                            "id": str(PLAYBOOK_ID),
                            "playbook_key": "0" * 64,
                            "version": 1,
                            "expected_at": NOW,
                            "superseded_at": None,
                        }
                    ]
                )
            ]
        )
        rows = list_due_playbooks(session, reference=NOW, limit=1000)
        self.assertEqual(len(rows), 1)
        statement, params = session.calls[0]
        lower = statement.lower()
        self.assertIn("where p.superseded_at is null", lower)
        self.assertIn("p.expected_at is null or p.expected_at <= :reference", lower)
        self.assertIn(
            "order by p.expected_at asc nulls last, p.created_at asc, p.id asc",
            " ".join(lower.split()),
        )
        self.assertIn("as entity_keys", lower)
        self.assertEqual(params["limit"], 100)  # clamped
        self.assertEqual(params["reference"], NOW)
        session.commit.assert_not_called()

    def test_history_requires_a_filter_and_orders_newest_first(self):
        with self.assertRaisesRegex(
            ValueError, "thesis_id or playbook_key is required"
        ):
            list_playbook_history(Session([]))
        session = Session([Result(rows=[])])
        list_playbook_history(session, thesis_id=THESIS_ID)
        statement, params = session.calls[0]
        lower = statement.lower()
        self.assertIn("where thesis_id = cast(:thesis_id as uuid)", lower)
        self.assertIn("order by playbook_key asc, version desc, id asc", lower)
        self.assertEqual(params["limit"], 50)
        session = Session([Result(rows=[])])
        list_playbook_history(session, playbook_key="0" * 64, limit=3)
        self.assertEqual(session.calls[0][1]["limit"], 3)
        session.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
