"""Tests for thesis autonomy production runner repair, identity backfill, and exact recovery safety."""

import copy
import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import bindparam, text
from sqlalchemy.dialects import postgresql

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from thesis_autonomy_support import (
    CANDIDATE,
    CITATION_FIELDS,
    EXISTING_ID,
    NOW,
    FakeStageFactory,
    LLMChallenger,
    LLMRoleRunner,
    LLMSemanticCitationAuditor,
    MemorySession,
    NormalizedEntity,
    RecordingSession,
    _id,
    _recovery_literal_value,
    _signal,
    attempt_rows,
    evidence_item,
    evidence_items,
    llm_result,
    role_output_schema,
)

from research_intelligence.evidence import exact_evidence_lookup
from thesis_autonomy import (
    _backfill_generated_catalysts,
    _backfill_missing_market_identities,
    _ensure_candidate_catalyst,
)


class ProductionRunnerRepairTests(unittest.TestCase):
    def _runner(self, session, budget=1.0):
        return LLMRoleRunner(
            {}, correlation_id="corr-1", session=session, budget_cap_usd=budget
        )

    def test_first_valid_response_uses_one_call_and_records_one_attempt(self):
        session = RecordingSession()
        factory = FakeStageFactory([llm_result([CANDIDATE])])
        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session)
            output = runner.run(role="fundamental", prompt="prompt", schema={})
        self.assertIsInstance(output, list)
        self.assertEqual(factory.calls, 1)
        rows = attempt_rows(session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "validated")
        self.assertEqual(rows[0]["attempt_number"], 1)
        self.assertEqual(rows[0]["processor"], "thesis_autonomy")
        self.assertEqual(rows[0]["stage"], "fundamental")
        self.assertEqual(rows[0]["validation_issues"], "[]")
        self.assertEqual(rows[0]["correlation_id"], "corr-1")
        self.assertEqual(runner.calls, 1)
        self.assertEqual(runner.cost_usd, 0.01)

    def test_malformed_then_valid_repairs_exactly_once(self):
        session = RecordingSession()
        factory = FakeStageFactory([llm_result("{not json"), llm_result([CANDIDATE])])
        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session)
            output = runner.run(role="contrarian", prompt="original", schema={})
        self.assertIsInstance(output, list)
        self.assertEqual(factory.calls, 2)
        self.assertIn("Repair the JSON once", factory.prompts[1])
        rows = attempt_rows(session)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["attempt_number"], 1)
        self.assertEqual(rows[0]["status"], "validation_failed")
        self.assertEqual(json.loads(rows[0]["validation_issues"]), ["JSONDecodeError"])
        self.assertEqual(rows[1]["attempt_number"], 2)
        self.assertEqual(rows[1]["status"], "validated")
        self.assertEqual(runner.calls, 2)

    def test_schema_incomplete_candidate_repairs_before_tournament(self):
        refs = [
            "source_claim:claim:0000",
            "source_claim:claim:0001",
            "source_claim:claim:0002",
        ]
        valid = copy.deepcopy(CANDIDATE)
        valid["evidence_refs"] = refs
        valid["citations"] = {field: refs for field in CITATION_FIELDS}
        invalid = copy.deepcopy(valid)
        invalid.pop("sentiment_context")
        session = RecordingSession()
        factory = FakeStageFactory([llm_result([invalid]), llm_result([valid])])

        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session)
            output = runner.run(
                role="fundamental",
                prompt="original",
                schema=role_output_schema(),
            )

        self.assertEqual(output, [valid])
        self.assertEqual(factory.calls, 2)
        self.assertIn("missing required fields: sentiment_context", factory.prompts[1])
        rows = attempt_rows(session)
        self.assertEqual(
            [row["status"] for row in rows], ["validation_failed", "validated"]
        )

    def test_malformed_twice_fails_soft_with_two_distinct_failed_attempts(self):
        session = RecordingSession()
        factory = FakeStageFactory([llm_result("{not json"), llm_result("42")])
        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session)
            with self.assertRaisesRegex(ValueError, "role output failed validation"):
                runner.run(role="editor", prompt="original", schema={})
        self.assertEqual(factory.calls, 2)
        rows = attempt_rows(session)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [row["status"] for row in rows],
            ["validation_failed", "validation_failed"],
        )
        self.assertEqual([row["attempt_number"] for row in rows], [1, 2])
        # Issues are bounded type names, never provider text. The second
        # payload is valid JSON but violates the required array shape.
        self.assertEqual(json.loads(rows[0]["validation_issues"]), ["JSONDecodeError"])
        self.assertEqual(
            json.loads(rows[1]["validation_issues"]),
            ["schema:output must be a JSON array"],
        )

    def test_shape_failure_is_repaired_but_semantic_failures_are_not(self):
        session = RecordingSession()
        factory = FakeStageFactory(
            [llm_result({"not": "an array"}), llm_result([CANDIDATE])]
        )
        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session)
            output = runner.run(role="macro_regime", prompt="p", schema={})
        self.assertIsInstance(output, list)
        rows = attempt_rows(session)
        self.assertEqual(rows[0]["status"], "validation_failed")
        self.assertEqual(
            json.loads(rows[0]["validation_issues"]),
            ["schema:output must be a JSON array"],
        )
        self.assertEqual(rows[1]["status"], "validated")

    def test_challenger_schema_failure_repairs_once(self):
        session = RecordingSession()
        factory = FakeStageFactory(
            [
                llm_result({"kind": "bogus", "statement": "x", "citations": []}),
                llm_result(None),
            ]
        )
        with patch("thesis_autonomy.LLMStage", factory):
            challenger = LLMChallenger(
                {}, correlation_id="corr-1", session=session, budget_cap_usd=1.0
            )
            snapshot = SimpleNamespace(
                thesis_id="t",
                statement="s",
                direction="long",
                as_of=NOW,
                cost=0.0,
                scenarios=(),
                conditions=(),
            )
            self.assertIsNone(challenger.challenge(snapshot, []))
        self.assertEqual(factory.calls, 2)
        rows = attempt_rows(session)
        self.assertEqual(
            [row["status"] for row in rows],
            ["validation_failed", "validated"],
        )
        self.assertEqual(rows[0]["stage"], "challenger")

    def test_challenger_receives_source_titles_and_excerpts(self):
        session = RecordingSession()
        item = evidence_item(1)
        factory = FakeStageFactory([llm_result(None)])
        with patch("thesis_autonomy.LLMStage", factory):
            challenger = LLMChallenger(
                {},
                correlation_id="corr-1",
                session=session,
                evidence_catalog={item.ref: item},
                budget_cap_usd=1.0,
            )
            snapshot = SimpleNamespace(
                thesis_id="t",
                statement="s",
                direction="long",
                as_of=NOW,
                cost=0.0,
                scenarios=(),
                conditions=(),
            )
            self.assertIsNone(challenger.challenge(snapshot, [_signal(item)]))

        self.assertIn(item.title, factory.prompts[0])
        self.assertIn(item.bounded_excerpt, factory.prompts[0])

    def test_auditor_uses_separate_stage_and_records_lineage(self):
        session = RecordingSession()
        decision = [
            {
                "candidate_key": "key-1",
                "verdict": "entailed",
                "cited_refs": ["source_claim:claim:0000"],
                "unsupported_claims": [],
                "rationale": "excerpt supports the claim",
            }
        ]
        factory = FakeStageFactory([llm_result(decision)])
        with patch("thesis_autonomy.LLMStage", factory):
            auditor = LLMSemanticCitationAuditor(
                {}, correlation_id="corr-1", session=session, budget_cap_usd=1.0
            )
            output = auditor.audit(
                candidates=[
                    {
                        "candidate_key": "key-1",
                        "claim": "claim text",
                        "evidence_refs": ["source_claim:claim:0000"],
                    }
                ],
                evidence={},
            )
        self.assertEqual(output, decision)
        self.assertEqual(factory.calls, 1)
        rows = attempt_rows(session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "citation_audit")
        self.assertEqual(rows[0]["status"], "validated")

    def test_auditor_repairs_exactly_once_and_fails_soft_on_double_malformed(self):
        session = RecordingSession()
        decision = [
            {
                "candidate_key": "key-1",
                "verdict": "entailed",
                "cited_refs": [],
                "unsupported_claims": [],
                "rationale": "ok",
            }
        ]
        factory = FakeStageFactory([llm_result("{bad"), llm_result(decision)])
        with patch("thesis_autonomy.LLMStage", factory):
            auditor = LLMSemanticCitationAuditor(
                {}, correlation_id="corr-1", session=session, budget_cap_usd=1.0
            )
            output = auditor.audit(candidates=[], evidence={})
        self.assertEqual(output, decision)
        self.assertEqual(factory.calls, 2)
        rows = attempt_rows(session)
        self.assertEqual([row["attempt_number"] for row in rows], [1, 2])
        self.assertEqual(
            [row["status"] for row in rows], ["validation_failed", "validated"]
        )

        session2 = RecordingSession()
        factory2 = FakeStageFactory([llm_result("{bad"), llm_result("[]x")])
        with patch("thesis_autonomy.LLMStage", factory2):
            auditor2 = LLMSemanticCitationAuditor(
                {}, correlation_id="corr-1", session=session2, budget_cap_usd=1.0
            )
            with self.assertRaisesRegex(ValueError, "citation audit output failed"):
                auditor2.audit(candidates=[], evidence={})
        self.assertEqual(factory2.calls, 2)

    def test_budget_cap_stops_further_calls(self):
        session = RecordingSession()
        factory = FakeStageFactory([llm_result([CANDIDATE], cost_usd=1.5)])
        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session, budget=1.0)
            with self.assertRaisesRegex(RuntimeError, "budget"):
                runner.run(role="fundamental", prompt="p", schema={})
        self.assertEqual(factory.calls, 1)


class MarketIdentityBackfillTests(unittest.TestCase):
    def test_legacy_null_identity_is_resolved_only_from_linked_evidence(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee growth "
                "through its fee-earning AUM engine"
            ),
        )
        items = [
            evidence_item(
                0,
                entities=[
                    NormalizedEntity.create(
                        "company",
                        "intermediate-capital-group-icg",
                        "Intermediate Capital Group (ICG)",
                    ),
                    NormalizedEntity.create("symbol", "icg-l", "ICG.L"),
                ],
            )
        ]
        session.seed_evidence(EXISTING_ID, evidence_id=items[0].evidence_id)
        catalog = {item.ref: item for item in items}
        changed = _backfill_missing_market_identities(session, catalog)

        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Intermediate Capital Group (ICG)")
        self.assertEqual(thesis["symbol"], "ICG.L")

    def test_unlinked_or_ambiguous_identity_is_never_invented(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim="Acme Corporation should sustain margin expansion",
        )

        changed = _backfill_missing_market_identities(
            session,
            {item.ref: item for item in evidence_items()},
        )

        self.assertEqual(changed, 0)
        self.assertIsNone(thesis["company"])
        self.assertIsNone(thesis["symbol"])

    def test_citation_outside_catalog_recovers_transcript_segment(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-icg:seg1",
        )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_transcripts",
            "institution": "Intermediate Capital Group",
            "document_type": "earnings_call",
            "title": "ICG earnings call",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg-call",
            "content": (
                "Operator: Welcome to the Intermediate Capital Group earnings "
                "call. Management: our fee-earning AUM engine grew this "
                "quarter, and guidance points to continued margin expansion."
            ),
            "metadata": {"ticker": "ICG.L", "available": True},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Intermediate Capital Group")
        self.assertEqual(thesis["symbol"], "ICG.L")

    def test_citation_outside_catalog_recovers_news_document(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Acme Industrial Holdings disclosed a new buyback program "
                "that should support margins"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-acme-news",
        )
        session.recovery_source_documents["doc-acme-news"] = {
            "document_id": "doc-acme-news",
            "source": "issuer_news",
            "institution": "Acme Industrial Holdings",
            "document_type": "issuer_update",
            "title": "Acme announces buyback",
            "published_at": NOW - timedelta(days=500),
            "url": "https://example.test/acme-news",
            "content": "Acme Industrial Holdings announced a buyback program.",
            "metadata": {"ticker": "ACME"},
            "created_at": NOW - timedelta(days=500),
            "updated_at": NOW - timedelta(days=500),
            "acquired_at": NOW - timedelta(days=500),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Acme Industrial Holdings")
        self.assertEqual(thesis["symbol"], "ACME")

    def test_citation_outside_catalog_recovers_filing_delta(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="filing_delta",
            evidence_id="delta-icg-1",
        )
        session.recovery_filing_deltas["delta-icg-1"] = {
            "id": "delta-icg-1",
            "document_id": "doc-icg-10k",
            "previous_document_id": "doc-icg-10k-prev",
            "category": "10-K",
            "change_kind": "modified",
            "section_hash": "h1",
            "previous_section_hash": "h0",
            "excerpt": "Fee-earning AUM grew.",
            "metrics": {},
            "created_at": NOW - timedelta(days=600),
            "company": "Intermediate Capital Group",
            "symbol": "ICG.L",
            "industry": "asset management",
            "region": "GB",
            "report_date": (NOW - timedelta(days=600)).date(),
            "source_url": "https://example.test/icg-10k",
            "filing_source": "sec",
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Intermediate Capital Group")
        self.assertEqual(thesis["symbol"], "ICG.L")

    def test_citation_outside_catalog_recovers_market_bar(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim="ICG.L should re-rate as fee-earning AUM compounds",
        )
        bar_time = NOW - timedelta(days=300)
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="market_confirmation",
            evidence_id=f"ICG.L:1d@{bar_time.isoformat()}",
        )
        session.recovery_market_data[("ICG.L", "1d", bar_time)] = {
            "symbol": "ICG.L",
            "timeframe": "1d",
            "timestamp": bar_time,
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 104.0,
            "volume": 1000.0,
            "source": "test",
            "metadata": {},
            "created_at": bar_time,
            "updated_at": bar_time,
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 1)
        self.assertIsNone(thesis["company"])
        self.assertEqual(thesis["symbol"], "ICG.L")

    def test_catalog_and_recovered_refs_merge_with_catalog_first(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        items = [
            evidence_item(
                0,
                entities=[
                    NormalizedEntity.create(
                        "company",
                        "intermediate-capital-group-icg",
                        "Intermediate Capital Group (ICG)",
                    ),
                    NormalizedEntity.create("symbol", "icg-l", "ICG.L"),
                ],
            )
        ]
        session.seed_evidence(EXISTING_ID, evidence_id=items[0].evidence_id)
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-icg",
        )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        changed = _backfill_missing_market_identities(
            session, {item.ref: item for item in items}, reference=NOW
        )

        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Intermediate Capital Group (ICG)")
        self.assertEqual(thesis["symbol"], "ICG.L")

    def test_recovered_ambiguous_identity_remains_unknown(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Alpha Holdings and Beta Holdings both support margin "
                "expansion this cycle"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-alpha",
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-beta",
        )
        for doc_id, institution, ticker in (
            ("doc-alpha", "Alpha Holdings", "ALPH.L"),
            ("doc-beta", "Beta Holdings", "BET.L"),
        ):
            session.recovery_source_documents[doc_id] = {
                "document_id": doc_id,
                "source": "issuer_news",
                "institution": institution,
                "document_type": "issuer_update",
                "title": f"{institution} update",
                "published_at": NOW - timedelta(days=400),
                "url": f"https://example.test/{doc_id}",
                "content": f"{institution} update",
                "metadata": {"ticker": ticker},
                "created_at": NOW - timedelta(days=400),
                "updated_at": NOW - timedelta(days=400),
                "acquired_at": NOW - timedelta(days=400),
            }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 0)
        self.assertIsNone(thesis["company"])
        self.assertIsNone(thesis["symbol"])

    def test_uncited_persisted_record_never_supplies_identity(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        # The source record exists in persistence, but no citation links it.
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 0)
        self.assertIsNone(thesis["company"])
        self.assertIsNone(thesis["symbol"])

    def test_citation_missing_from_persistence_remains_unknown(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-other",
        )
        # Only an unrelated document is persisted; the cited one is gone.
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 0)
        self.assertIsNone(thesis["company"])
        self.assertIsNone(thesis["symbol"])

    def test_record_persisted_after_reference_is_not_recovered(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-icg",
        )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW + timedelta(days=1),
            "updated_at": NOW + timedelta(days=1),
            "acquired_at": NOW + timedelta(days=1),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 0)
        self.assertIsNone(thesis["company"])
        self.assertIsNone(thesis["symbol"])
        # Without a reference bound the same record is available and
        # resolves (backward-compatible default).
        changed = _backfill_missing_market_identities(session, {})
        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Intermediate Capital Group")

    def test_same_identity_symbol_canonicalized_different_identity_preserved(self):
        session = MemorySession()
        curated = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            symbol="icg.l",
        )
        different = session.seed_thesis(
            _id("legacy-different-symbol"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            symbol="MSFT",
        )
        for thesis_id in (EXISTING_ID, _id("legacy-different-symbol")):
            session.seed_evidence(
                thesis_id,
                evidence_type="official_document",
                evidence_id="doc-icg",
            )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 2)
        self.assertEqual(curated["company"], "Intermediate Capital Group")
        self.assertEqual(curated["symbol"], "ICG.L")
        self.assertEqual(different["company"], "Intermediate Capital Group")
        self.assertEqual(different["symbol"], "MSFT")

    def test_backfill_with_recovery_is_idempotent(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-icg",
        )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        first = _backfill_missing_market_identities(session, {}, reference=NOW)
        second = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

    def _seed_icg_recovery(self, session: MemorySession) -> None:
        """Seed the exact-ID recovery record and its citation."""
        for thesis_id in (
            _id("legacy-post-created"),
            _id("legacy-post-updated"),
            _id("legacy-post-fused"),
            EXISTING_ID,
        ):
            session.seed_evidence(
                thesis_id,
                evidence_type="official_document",
                evidence_id="doc-icg",
            )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }

    def test_post_reference_thesis_never_gets_identity_backfilled(self):
        # An older/stale job must not rewrite identity state on a thesis
        # that did not exist at its reference, was updated after it, or
        # was fused after it; a reference-visible legacy row still
        # backfills exactly once.
        session = MemorySession()
        post_created = session.seed_thesis(
            _id("legacy-post-created"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            created_at=NOW,
            updated_at=NOW,
        )
        post_updated = session.seed_thesis(
            _id("legacy-post-updated"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            created_at=NOW - timedelta(days=2),
            updated_at=NOW,
        )
        post_fused = session.seed_thesis(
            _id("legacy-post-fused"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
            fusion_reference_at=NOW,
        )
        visible = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        self._seed_icg_recovery(session)

        changed = _backfill_missing_market_identities(
            session, {}, reference=NOW - timedelta(days=1)
        )

        self.assertEqual(changed, 1)
        for thesis in (post_created, post_updated, post_fused):
            self.assertIsNone(thesis["company"])
            self.assertIsNone(thesis["symbol"])
        # The visible legacy row backfills exactly once from the same
        # exact-ID recovery (the excluded rows never mutated it).
        self.assertEqual(visible["company"], "Intermediate Capital Group")
        self.assertEqual(visible["symbol"], "ICG.L")

    def test_missing_thesis_timestamps_fail_closed_for_identity_backfill(self):
        # A thesis without a provable created/updated timestamp is never
        # selected for identity backfill (mirrors the production NULL
        # comparison); the exact cited-evidence point-in-time gate is
        # irrelevant because the thesis row itself is not visible.
        session = MemorySession()
        no_created = session.seed_thesis(
            _id("legacy-no-created"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            created_at=None,
            updated_at=NOW - timedelta(days=2),
        )
        no_updated = session.seed_thesis(
            _id("legacy-no-updated"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            created_at=NOW - timedelta(days=2),
            updated_at=None,
        )
        for thesis_id in (_id("legacy-no-created"), _id("legacy-no-updated")):
            session.seed_evidence(
                thesis_id,
                evidence_type="official_document",
                evidence_id="doc-icg",
            )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }

        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 0)
        self.assertIsNone(no_created["company"])
        self.assertIsNone(no_created["symbol"])
        self.assertIsNone(no_updated["company"])
        self.assertIsNone(no_updated["symbol"])


class ExactRecoverySafetyTests(unittest.TestCase):
    """Exact-ID recovery hardening: PostgreSQL-valid binds, savepoint
    isolation for each physical source query, and the final common
    point-in-time gate that no builder's availability semantics bypass."""

    BAR_AT = datetime(2026, 1, 10, tzinfo=UTC)

    def _seed_all_recovery_sources(self, session: MemorySession) -> list[str]:
        """Seed one valid persisted record per recovery source table and
        return the exact refs that cite them."""
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        session.recovery_research_source_claims["claim:1"] = {
            "id": "claim:1",
            "evidence_type": "fundamental",
            "evidence_id": None,
            "subject": "Acme Corporation",
            "predicate": "disclosed margin expansion",
            "object_value": None,
            "unit": None,
            "period": None,
            "geography": None,
            "direction": None,
            "claim_kind": "trend",
            "source_span": "Costs are falling",
            "observed_at": NOW - timedelta(days=30),
            "confidence": 0.6,
            "entities": [],
            "model_slug": "test",
            "prompt_version": "v1",
            "input_fingerprint": "fp",
            "provenance": {},
            "created_at": NOW - timedelta(days=30),
        }
        session.recovery_filing_deltas["delta-icg-1"] = {
            "id": "delta-icg-1",
            "document_id": "doc-icg-10k",
            "previous_document_id": "doc-icg-10k-prev",
            "category": "10-K",
            "change_kind": "modified",
            "section_hash": "h1",
            "previous_section_hash": "h0",
            "excerpt": "Fee-earning AUM grew.",
            "metrics": {},
            "created_at": NOW - timedelta(days=600),
            "company": "Intermediate Capital Group",
            "symbol": "ICG.L",
            "industry": "asset management",
            "region": "GB",
            "report_date": (NOW - timedelta(days=600)).date(),
            "source_url": "https://example.test/icg-10k",
            "filing_source": "sec",
        }
        session.recovery_observations["obs-1"] = {
            "observation_id": "obs-1",
            "source_kind": "fundamental",
            "source_id": "src-1",
            "observed_at": NOW - timedelta(days=20),
            "industry": "asset management",
            "company": "Intermediate Capital Group",
            "symbol": "ICG.L",
            "region": "GB",
            "metrics": {},
            "narrative": {"summary": "AUM grew"},
            "themes": [],
            "score": 0.5,
            "state": "current",
            "provenance": {},
            "created_at": NOW - timedelta(days=20),
            "updated_at": NOW - timedelta(days=20),
        }
        session.recovery_analyses["ana-1"] = {
            "analysis_id": "ana-1",
            "document_id": "doc-icg-10k",
            "previous_document_id": "doc-icg-10k-prev",
            "facts": {"metrics": {}, "qualitative": {}},
            "analysis": {"summary": "Margins expand", "state": "complete"},
            "model": "test",
            "created_at": NOW - timedelta(days=15),
            "updated_at": NOW - timedelta(days=15),
            "company": "Intermediate Capital Group",
            "symbol": "ICG.L",
            "industry": "asset management",
            "region": "GB",
            "document_type": "10-K",
            "report_date": (NOW - timedelta(days=600)).date(),
            "source_url": "https://example.test/icg-10k",
            "filing_source": "sec",
        }
        session.recovery_market_data[("ICG.L", "1d", self.BAR_AT)] = {
            "symbol": "ICG.L",
            "timeframe": "1d",
            "timestamp": self.BAR_AT,
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 104.0,
            "volume": 1000.0,
            "source": "test",
            "metadata": {},
            "created_at": self.BAR_AT,
            "updated_at": self.BAR_AT,
        }
        session.recovery_option_snapshots[("cboe", "ICG.L", self.BAR_AT)] = {
            "source": "cboe",
            "symbol": "ICG.L",
            "captured_at": self.BAR_AT,
            "source_timestamp": self.BAR_AT,
            "created_at": self.BAR_AT,
        }
        session.recovery_positioning[
            ("finra", "ICG.L", self.BAR_AT.date(), "short_volume")
        ] = {
            "source": "finra",
            "market_id": "ICG.L",
            "report_date": self.BAR_AT.date(),
            "category": "short_volume",
            "metadata": {"positioning_kind": "short_volume"},
            "created_at": self.BAR_AT,
            "updated_at": self.BAR_AT,
            "acquired_at": self.BAR_AT,
        }
        session.recovery_corporate_actions["ca-1"] = {
            "action_id": "ca-1",
            "symbol": "ICG.L",
            "action_type": "dividend",
            "effective_date": self.BAR_AT.date(),
            "source": "test",
            "source_timestamp": self.BAR_AT,
            "available_at": self.BAR_AT,
            "description": "Q1 dividend",
            "metadata": {},
            "created_at": self.BAR_AT,
        }
        session.recovery_story_clusters["cl-1"] = {
            "id": "cl-1",
            "title": "ICG story",
            "summary": "AUM growth",
            "state": "forming",
            "lane": "single_name",
            "first_seen_at": (self.BAR_AT - timedelta(days=1)).isoformat(),
            "last_seen_at": (self.BAR_AT - timedelta(days=1)).isoformat(),
            "last_material_change_at": (self.BAR_AT - timedelta(days=1)).isoformat(),
            "importance": 0.5,
            "novelty": 0.4,
            "confidence": 0.6,
            "source_count": 3,
            "version": 2,
            "change_summary": None,
            "entities": [],
            "markets": [],
            "clustering_reason": {},
            "updated_at": self.BAR_AT - timedelta(days=1),
        }
        return [
            "official_document:doc-icg",
            "source_claim:claim:1",
            "filing_delta:delta-icg-1",
            "investment_observation:obs-1",
            "investment_analysis:ana-1",
            f"market_confirmation:ICG.L:1d@{self.BAR_AT.isoformat()}",
            f"market_confirmation:cboe:ICG.L@{self.BAR_AT.isoformat()}",
            (
                "market_confirmation:finra:ICG.L:"
                f"{self.BAR_AT.date().isoformat()}:short_volume"
            ),
            "market_confirmation:ca-1",
            "story_cluster:cl-1",
        ]

    def test_recovery_statements_compile_with_postgresql_typed_binds(self):
        session = MemorySession()
        refs = self._seed_all_recovery_sources(session)
        recovered = exact_evidence_lookup(session, refs, available_by=NOW)

        self.assertEqual(set(refs), set(recovered))
        calls = [
            (sql, params)
            for sql, params in session.calls
            if "autonomy_identity_recovery" in sql
        ]
        self.assertGreaterEqual(len(calls), 10)
        self.assertEqual(session.savepoints, len(calls))
        self.assertEqual(session.savepoint_rollbacks, 0)

        array_element_types = {
            "ids": postgresql.TEXT,
            "symbols": postgresql.TEXT,
            "timeframes": postgresql.TEXT,
            "sources": postgresql.TEXT,
            "market_ids": postgresql.TEXT,
            "categories": postgresql.TEXT,
            "report_dates": postgresql.DATE,
            "timestamps": postgresql.TIMESTAMP(timezone=True),
            "captured_ats": postgresql.TIMESTAMP(timezone=True),
        }
        for sql, params in calls:
            # Every supplied parameter must be a recognized text bind; the
            # unsafe ':bind::TYPE[]' spelling silently mis-parses binds, so
            # execution would fail instead of binding the arrays.
            compiled = text(sql).compile(dialect=postgresql.dialect())
            self.assertEqual(set(params), set(compiled.params), sql)
            self.assertNotRegex(sql, r":\w+::(TEXT|TIMESTAMPTZ|DATE)\[")
            if "CAST(:" in sql:
                typed = [
                    bindparam(
                        name,
                        value=(
                            _recovery_literal_value(
                                name, value, array_element_types.get(name)
                            )
                            if name in array_element_types
                            else value
                        ),
                        type_=(
                            postgresql.ARRAY(array_element_types[name])
                            if name in array_element_types
                            else postgresql.TIMESTAMP(timezone=True)
                            if name == "available_by"
                            else None
                        ),
                    )
                    for name, value in params.items()
                ]
                rendered = (
                    text(sql)
                    .bindparams(*typed)
                    .compile(
                        dialect=postgresql.dialect(),
                        compile_kwargs={"literal_binds": True},
                    )
                    .string
                )
                self.assertIn("CAST(ARRAY[", rendered, sql)

    def test_source_query_failure_rolls_back_savepoint_and_keeps_other_sources(self):
        session = MemorySession()
        refs = self._seed_all_recovery_sources(session)
        session.failing_recovery_tables = {"source_documents"}

        recovered = exact_evidence_lookup(session, refs, available_by=NOW)

        self.assertNotIn("official_document:doc-icg", recovered)
        self.assertEqual(set(refs) - {"official_document:doc-icg"}, set(recovered))
        self.assertEqual(session.savepoint_rollbacks, 1)
        # The outer transaction survived: a later lookup on the same
        # session recovers the ref the broken table previously withheld.
        session.failing_recovery_tables = set()
        recovered = exact_evidence_lookup(
            session, ["official_document:doc-icg"], available_by=NOW
        )
        self.assertEqual(set(recovered), {"official_document:doc-icg"})
        self.assertEqual(session.savepoint_rollbacks, 1)

    def test_partial_market_recovery_survives_later_market_table_failure(self):
        session = MemorySession()
        self._seed_all_recovery_sources(session)
        bar_ref = f"market_confirmation:ICG.L:1d@{self.BAR_AT.isoformat()}"
        option_ref = f"market_confirmation:cboe:ICG.L@{self.BAR_AT.isoformat()}"
        corporate_ref = "market_confirmation:ca-1"
        session.failing_recovery_tables = {"option_chain_snapshots"}

        recovered = exact_evidence_lookup(
            session, [bar_ref, option_ref, corporate_ref], available_by=NOW
        )

        self.assertEqual(set(recovered), {bar_ref, corporate_ref})
        self.assertEqual(session.savepoint_rollbacks, 1)

    def test_partial_market_recovery_survives_first_market_table_failure(self):
        session = MemorySession()
        self._seed_all_recovery_sources(session)
        bar_ref = f"market_confirmation:ICG.L:1d@{self.BAR_AT.isoformat()}"
        option_ref = f"market_confirmation:cboe:ICG.L@{self.BAR_AT.isoformat()}"
        corporate_ref = "market_confirmation:ca-1"
        session.failing_recovery_tables = {"market_data"}

        recovered = exact_evidence_lookup(
            session, [bar_ref, option_ref, corporate_ref], available_by=NOW
        )

        self.assertEqual(set(recovered), {option_ref, corporate_ref})
        self.assertEqual(session.savepoint_rollbacks, 1)

    def test_future_source_or_availability_excluded_by_final_cutoff(self):
        session = MemorySession()
        # A claim persisted before the cutoff whose observed timestamp lies
        # after it (the table filter can only bound created_at).
        session.recovery_research_source_claims["claim:future-observed"] = {
            "id": "claim:future-observed",
            "evidence_type": "fundamental",
            "evidence_id": None,
            "subject": "Acme Corporation",
            "predicate": "disclosed margin expansion",
            "object_value": None,
            "unit": None,
            "period": None,
            "geography": None,
            "direction": None,
            "claim_kind": "trend",
            "source_span": "Costs are falling",
            "observed_at": NOW + timedelta(days=1),
            "confidence": 0.6,
            "entities": [],
            "model_slug": "test",
            "prompt_version": "v1",
            "input_fingerprint": "fp",
            "provenance": {},
            "created_at": NOW - timedelta(days=30),
        }
        # A story version persisted before the cutoff whose snapshot
        # last_seen_at lies after it.
        session.recovery_story_clusters["cl-future"] = {
            "id": "cl-future",
            "title": "Future story",
            "summary": "Later sighting",
            "state": "forming",
            "lane": "single_name",
            "first_seen_at": (NOW - timedelta(days=2)).isoformat(),
            "last_seen_at": (NOW + timedelta(days=1)).isoformat(),
            "last_material_change_at": (NOW - timedelta(days=2)).isoformat(),
            "importance": 0.5,
            "novelty": 0.4,
            "confidence": 0.6,
            "source_count": 3,
            "version": 2,
            "change_summary": None,
            "entities": [],
            "markets": [],
            "clustering_reason": {},
            "updated_at": NOW - timedelta(days=2),
        }
        # A corporate action whose source time predates the cutoff but whose
        # availability time does not.
        session.recovery_corporate_actions["ca-future"] = {
            "action_id": "ca-future",
            "symbol": "ICG.L",
            "action_type": "dividend",
            "effective_date": (NOW - timedelta(days=2)).date(),
            "source": "test",
            "source_timestamp": NOW - timedelta(days=2),
            "available_at": NOW + timedelta(days=1),
            "description": "Q1 dividend",
            "metadata": {},
            "created_at": NOW - timedelta(days=2),
        }
        refs = [
            "source_claim:claim:future-observed",
            "story_cluster:cl-future",
            "market_confirmation:ca-future",
        ]

        bounded = exact_evidence_lookup(session, refs, available_by=NOW)
        self.assertEqual(bounded, {})
        self.assertEqual(session.savepoint_rollbacks, 0)
        # Without a cutoff the same records satisfy the contract
        # (backward-compatible default), proving the final gate alone
        # excludes them while bounded.
        unbounded = exact_evidence_lookup(session, refs)
        self.assertEqual(set(unbounded), set(refs))


class CandidateCatalystIdentityTests(unittest.TestCase):
    """Guarded exact-identity persistence for generated catalysts."""

    def test_guard_takes_namespaced_identity_lock_before_the_insert(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        changed = _ensure_candidate_catalyst(
            session, EXISTING_ID, "  A   quarterly disclosure  "
        )
        self.assertTrue(changed)
        # The exact identity lock is namespaced apart from the fusion
        # canonical-key lock and precedes the insert.
        self.assertEqual(
            session.catalyst_lock_keys,
            [f"catalyst_identity:{EXISTING_ID}:A quarterly disclosure"],
        )
        # Global lock order: the fusion canonical-key lock (merge's exact
        # lock) comes first, then the catalyst identity lock, then the
        # guarded insert — a catalyst lock is never retained across a
        # later fusion acquisition.
        fusion_index = next(
            index
            for index, (sql, params) in enumerate(session.calls)
            if "pg_advisory_xact_lock" in sql
            and "catalyst_identity_lock" not in sql
            and params.get("key") == f"key:{EXISTING_ID}"
        )
        lock_index = next(
            index
            for index, (sql, _params) in enumerate(session.calls)
            if "catalyst_identity_lock" in sql
        )
        insert_index = next(
            index
            for index, (sql, _params) in enumerate(session.calls)
            if sql.startswith("INSERT INTO investment_catalysts")
        )
        self.assertLess(fusion_index, lock_index)
        self.assertLess(lock_index, insert_index)
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["state"], "pending")

    def test_legacy_thesis_without_canonical_key_skips_the_fusion_lock(self):
        # A legacy thesis with no canonical identity cannot be merged by a
        # concurrent cycle, so the guarded helper takes only the catalyst
        # identity lock: no fusion lock to deadlock against.
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, canonical_key=None)
        self.assertTrue(
            _ensure_candidate_catalyst(session, EXISTING_ID, "Legacy catalyst")
        )
        self.assertEqual(
            session.catalyst_lock_keys,
            [f"catalyst_identity:{EXISTING_ID}:Legacy catalyst"],
        )
        self.assertFalse(
            any(
                "pg_advisory_xact_lock" in sql and "catalyst_identity_lock" not in sql
                for sql, _params in session.calls
            )
        )
        self.assertEqual(len(session.catalysts), 1)

    def test_rerun_takes_the_lock_again_and_reports_a_truthful_noop(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        description = "Quarterly disclosure confirms the operating change"
        self.assertTrue(_ensure_candidate_catalyst(session, EXISTING_ID, description))
        self.assertFalse(_ensure_candidate_catalyst(session, EXISTING_ID, description))
        # No-op reruns still serialize on the same identity lock, so a
        # concurrent rerun cannot slip past a fresh insert.
        self.assertEqual(
            session.catalyst_lock_keys,
            [
                f"catalyst_identity:{EXISTING_ID}:{description}",
                f"catalyst_identity:{EXISTING_ID}:{description}",
            ],
        )
        self.assertEqual(len(session.catalysts), 1)

    def test_distinct_descriptions_stay_separate_rows(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        self.assertTrue(
            _ensure_candidate_catalyst(session, EXISTING_ID, "First catalyst")
        )
        self.assertTrue(
            _ensure_candidate_catalyst(session, EXISTING_ID, "Second catalyst")
        )
        # Different identities lock different keys and persist separately.
        self.assertEqual(
            session.catalyst_lock_keys,
            [
                f"catalyst_identity:{EXISTING_ID}:First catalyst",
                f"catalyst_identity:{EXISTING_ID}:Second catalyst",
            ],
        )
        self.assertEqual(len(session.catalysts), 2)

    def test_blank_description_is_rejected_before_taking_the_lock(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        for blank in (None, "", "   "):
            self.assertFalse(_ensure_candidate_catalyst(session, EXISTING_ID, blank))
        self.assertEqual(session.catalyst_lock_keys, [])
        self.assertEqual(len(session.catalysts), 0)


class GeneratedCatalystBackfillTests(unittest.TestCase):
    def test_reference_visible_legacy_catalyst_is_materialized_once(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            catalyst_summary="Quarterly disclosure confirms the operating change",
        )

        first = _backfill_generated_catalysts(session, NOW)
        second = _backfill_generated_catalysts(session, NOW)

        # (thesis_id, normalized summary) pairs feed the current-cycle
        # explicit scoring inputs for the backfilled catalysts.
        self.assertEqual(
            first,
            (
                (
                    EXISTING_ID,
                    "Quarterly disclosure confirms the operating change",
                ),
            ),
        )
        self.assertEqual(second, ())
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["state"], "pending")

    def test_post_reference_created_thesis_is_never_backfilled(self):
        # A replay at R must not materialize a catalyst for a thesis that
        # did not exist at R; a reference-visible thesis still backfills.
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            catalyst_summary="Quarterly disclosure confirms the operating change",
            created_at=NOW,
            updated_at=NOW,
        )
        visible_id = _id("legacy-visible-catalyst")
        session.seed_thesis(
            visible_id,
            catalyst_summary="Visible legacy summary",
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
        )

        result = _backfill_generated_catalysts(session, NOW - timedelta(days=1))

        self.assertEqual(result, ((visible_id, "Visible legacy summary"),))
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["thesis_id"], visible_id)

    def test_post_reference_updated_thesis_is_never_backfilled(self):
        # An older job must not materialize catalyst state on a thesis a
        # newer cycle updated after the reference (existence alone is not
        # enough: the current row must be provably visible at R).
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            catalyst_summary="Quarterly disclosure confirms the operating change",
            created_at=NOW - timedelta(days=2),
            updated_at=NOW,
        )
        visible_id = _id("legacy-visible-catalyst")
        session.seed_thesis(
            visible_id,
            catalyst_summary="Visible legacy summary",
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
        )

        result = _backfill_generated_catalysts(session, NOW - timedelta(days=1))

        self.assertEqual(result, ((visible_id, "Visible legacy summary"),))
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["thesis_id"], visible_id)

    def test_future_fusion_reference_thesis_is_never_backfilled(self):
        # A thesis claimed by a newer accepted reference is never
        # backfilled by an older replay, whatever its lifecycle timestamps.
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            catalyst_summary="Quarterly disclosure confirms the operating change",
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
            fusion_reference_at=NOW,
        )
        visible_id = _id("legacy-visible-catalyst")
        session.seed_thesis(
            visible_id,
            catalyst_summary="Visible legacy summary",
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
        )

        result = _backfill_generated_catalysts(session, NOW - timedelta(days=1))

        self.assertEqual(result, ((visible_id, "Visible legacy summary"),))
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["thesis_id"], visible_id)

    def test_missing_timestamps_fail_closed_for_catalyst_backfill(self):
        # A thesis without a provable created/updated timestamp is never
        # selected for backfill (mirrors the production NULL comparison).
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            catalyst_summary="Quarterly disclosure confirms the operating change",
            created_at=None,
            updated_at=NOW - timedelta(days=2),
        )
        session.seed_thesis(
            _id("legacy-no-updated"),
            catalyst_summary="Another legacy summary",
            created_at=NOW - timedelta(days=2),
            updated_at=None,
        )
        session.seed_thesis(
            _id("legacy-no-created"),
            catalyst_summary="Yet another legacy summary",
            created_at=None,
            updated_at=None,
        )

        result = _backfill_generated_catalysts(session, NOW)

        self.assertEqual(result, ())
        self.assertEqual(session.catalysts, [])


if __name__ == '__main__':
    unittest.main()
