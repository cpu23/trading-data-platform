import json
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from research_intelligence.adversarial import validate_adversarial_output  # noqa: E402
from research_intelligence.claims import validate_claim_output  # noqa: E402
from research_intelligence.config import ResearchSettings  # noqa: E402
from research_intelligence.contracts import (  # noqa: E402
    VALUE_CAPTURE_DIMENSIONS,
    ModelProvenance,
    NormalizedEvidence,
)
from research_intelligence.deliverable import validate_deliverable_output  # noqa: E402
from research_intelligence.discovery import (  # noqa: E402
    build_candidate_groups,
    select_case_match,
    validate_pattern_output,
)
from research_intelligence.evidence import (  # noqa: E402
    EvidenceCollection,
    EvidenceRegistry,
    MacroReleaseAdapter,
    MarketConfirmationAdapter,
    OfficialDocumentAdapter,
)
from research_intelligence.graph import (  # noqa: E402
    bounded_traversal,
    validate_causal_output,
)
from research_intelligence.lifecycle import (  # noqa: E402
    CaseStats,
    next_lifecycle_state,
)
from research_intelligence.market_drivers import (  # noqa: E402
    validate_factor_market_output,
    validate_market_driver_output,
)
from research_intelligence.models import (  # noqa: E402
    STAGE_SCHEMAS,
    STAGE_VERSIONS,
    ResearchModelRunner,
    ResearchModelValidationError,
    ResearchRunBudgetExceeded,
)
from research_intelligence.relationships import (  # noqa: E402
    causal_edge_fingerprint,
    normalize_entity,
    validate_relationship,
)
from research_intelligence.repository import (  # noqa: E402
    persist_adversarial,
    persist_economic_factors,
    persist_market_drivers,
    promote_case_to_theme,
    refresh_case_lifecycles,
    unresolved_material_hypotheses,
    upsert_case,
)
from research_intelligence.service import (  # noqa: E402
    run_case_update,
    run_discovery,
    run_macro_transmission,
)
from research_intelligence.value_capture import (
    validate_value_capture_output,  # noqa: E402
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
THEME_ID = UUID("22222222-2222-4222-8222-222222222222")


def settings(**overrides):
    values = {
        "enabled": True,
        "graph": {"depth": 5, "hard_depth": 5, "maximum_nodes": 80, "maximum_edges": 80},
        "limits": {
            "maximum_candidate_evidence": 100,
            "maximum_cases_per_run": 10,
            "evidence_per_candidate": 30,
        },
        "discovery": {
            "minimum_evidence_count": 3,
            "minimum_source_diversity": 2,
        },
    }
    values.update(overrides)
    return ResearchSettings.from_config({"research_intelligence": values})


def evidence_from_fixture(raw):
    entities = tuple(
        normalize_entity(item["entity_type"], item["name"])
        for item in raw.get("entities", [])
    )
    return NormalizedEvidence.create(
        evidence_type=raw["evidence_type"],
        evidence_id=raw["evidence_id"],
        source_name=raw["source_name"],
        source_timestamp=raw["source_timestamp"],
        acquired_at=raw["source_timestamp"],
        title=raw["title"],
        bounded_excerpt=raw.get("bounded_excerpt"),
        source_reference=f"fixture://{raw['evidence_id']}",
        entities=entities,
        structured_fields=raw.get("structured_fields", {}),
        provenance={"adapter": "fixture"},
        freshness="current",
    )


def strict_pattern(raw):
    rationale = {
        key: f"Supplied evidence supports the {key.replace('_', ' ')} assessment."
        for key in raw["importance"]
    }
    return {
        "abstained": False,
        "coherent": True,
        **raw,
        "importance_rationale": rationale,
    }


def strict_edges(raw_edges):
    return {
        "abstained": False,
        "edges": [
            {
                **raw,
                "confidence": 0.8 if raw["epistemic_state"] != "hypothesis" else None,
                "valid_from": None,
                "valid_to": None,
            }
            for raw in raw_edges
        ],
    }


def strict_capture(raw_capture):
    assessments = []
    for raw in raw_capture:
        dimensions = {key: None for key in VALUE_CAPTURE_DIMENSIONS}
        dimensions.update(raw["dimensions"])
        rationale = {key: "" for key in VALUE_CAPTURE_DIMENSIONS}
        rationale.update(raw["rationale"])
        assessments.append(
            {
                "node": raw["node"],
                "dimensions": dimensions,
                "rationale": rationale,
                "evidence_ids": raw["evidence_ids"],
                "unknowns": raw["unknowns"],
            }
        )
    return {"abstained": False, "assessments": assessments}


def edge_fingerprints(edges):
    return tuple(
        causal_edge_fingerprint(
            from_type=edge.from_type,
            from_key=edge.from_key,
            relationship=edge.relationship,
            to_type=edge.to_type,
            to_key=edge.to_key,
        )
        for edge in edges
    )


def strict_adversarial(raw, fingerprints):
    counters = []
    for item in raw["counterevidence"]:
        edge_index = item.pop("edge_index")
        counters.append(
            {
                **item,
                "edge_fingerprint": fingerprints[edge_index] if edge_index is not None else None,
            }
        )
    weakest_index = raw["weakest_edge_index"]
    return {
        "abstained": False,
        "counterevidence": counters,
        "data_requests": raw["data_requests"],
        "invalidation_conditions": raw["invalidation_conditions"],
        "strengthening_observations": raw["strengthening_observations"],
        "weakest_edge_fingerprint": fingerprints[weakest_index]
        if weakest_index is not None
        else None,
    }


def strict_deliverable(raw, fingerprints):
    return {
        "abstained": False,
        "what_changed": raw["what_changed"],
        "why_it_matters": raw["why_it_matters"],
        "transmission": {
            "text": raw["transmission_text"],
            "edge_fingerprints": [
                fingerprints[index] for index in raw["transmission_edge_indexes"]
            ],
        },
        "potential_capture": raw["potential_capture"],
        "evidence_for": raw["evidence_for"],
        "evidence_against": raw["evidence_against"],
        "weak_links_unknowns": raw["weak_links_unknowns"],
        "what_to_watch": raw["what_to_watch"],
    }


def load_scenario(filename):
    raw = json.loads((FIXTURES / filename).read_text())
    evidence = tuple(evidence_from_fixture(item) for item in raw["evidence"])
    groups = build_candidate_groups(evidence, settings(), maximum_groups=30)
    group = max(groups, key=lambda item: len(item.evidence))
    if len(group.evidence) != len(evidence):
        raise AssertionError("fixture did not deterministically produce one full evidence block")
    pattern = validate_pattern_output(strict_pattern(dict(raw["pattern"])), group)
    edges = validate_causal_output(
        strict_edges(raw["edges"]), evidence, settings(), seed_entities=pattern.entities
    )
    fingerprints = edge_fingerprints(edges)
    capture = validate_value_capture_output(strict_capture(raw["capture"]), evidence)
    adversarial_raw = json.loads(json.dumps(raw["adversarial"]))
    adversarial = validate_adversarial_output(
        strict_adversarial(adversarial_raw, fingerprints),
        evidence,
        edge_fingerprints=fingerprints,
    )
    deliverable = validate_deliverable_output(
        strict_deliverable(raw["deliverable"], fingerprints),
        evidence,
        edge_fingerprints=fingerprints,
        assessment_nodes=[(item.node_type, item.node_key) for item in capture],
    )
    return raw, evidence, group, pattern, edges, capture, adversarial, deliverable


class Result:
    def __init__(self, first=None, rows=None, rowcount=0):
        self._first = first
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._rows


class CaseSession:
    def __init__(self):
        self.case = None
        self.calls = []
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        self.calls.append((sql, params))
        if "SELECT * FROM research_cases WHERE semantic_fingerprint" in sql:
            return Result(first=dict(self.case) if self.case else None)
        if "SELECT * FROM research_cases WHERE id" in sql:
            return Result(first=dict(self.case) if self.case else None)
        if "INSERT INTO research_cases" in sql:
            self.case = {
                "id": CASE_ID,
                "input_fingerprint": params["input_fingerprint"],
                "title": params["title"],
            }
            return Result(first={"id": CASE_ID})
        if "UPDATE research_cases SET" in sql:
            self.case.update(
                input_fingerprint=params["input_fingerprint"], title=params["title"]
            )
            return Result(rowcount=1)
        return Result()


class EvidenceAndClaimTests(unittest.TestCase):
    def test_contract_is_immutable_deterministic_and_bounded(self):
        item = NormalizedEvidence.create(
            evidence_type="story_cluster",
            evidence_id="story-1",
            source_name="wire",
            source_timestamp=NOW,
            title="A" * 500,
            bounded_excerpt="B" * 5000,
            entities=(normalize_entity("sector", "Grid Equipment"),),
            structured_fields={"nested": ["safe"]},
            provenance={"run": "fixture"},
        )
        self.assertEqual(item.to_dict()["evidence_ref"], item.ref)
        again = NormalizedEvidence.create(
            evidence_type="story_cluster",
            evidence_id="story-1",
            source_name="wire",
            source_timestamp=NOW,
            title="A" * 500,
            bounded_excerpt="B" * 5000,
            entities=(normalize_entity("industry", "Grid Equipment"),),
            structured_fields={"nested": ["safe"]},
            provenance={"run": "different provenance"},
        )
        self.assertEqual(item.content_fingerprint, again.content_fingerprint)
        self.assertEqual(item.entities[0].entity_type, "industry")
        self.assertLessEqual(len(item.title), 300)
        self.assertLessEqual(len(item.bounded_excerpt), 1500)
        with self.assertRaises(FrozenInstanceError):
            item.title = "changed"
        with self.assertRaises(TypeError):
            item.structured_fields["unsafe"] = True

    def test_registry_isolates_adapter_failure_dedupes_and_enforces_limit(self):
        item = evidence_from_fixture(
            {
                "evidence_type": "story_cluster",
                "evidence_id": "same",
                "source_name": "wire",
                "source_timestamp": NOW.isoformat(),
                "title": "Grid equipment backlog",
                "bounded_excerpt": "Grid equipment backlog remains firm.",
            }
        )

        class Good:
            name = "good"

            def collect(self, session, *, since, until=None, limit):
                self.seen = (since, until, limit)
                return [item, item]

        class Bad:
            name = "bad"

            def collect(self, session, *, since, until=None, limit):
                raise RuntimeError("source unavailable")

        good = Good()
        result = EvidenceRegistry((good, Bad())).collect(
            object(), rolling_window_days=9999, limit=1, now=NOW
        )
        self.assertEqual(result.items, (item,))
        self.assertEqual(result.failures, {"bad": "RuntimeError"})
        self.assertGreaterEqual(good.seen[0], NOW - timedelta(days=730))
        self.assertEqual(good.seen[2], 1)

    def test_claim_extraction_preserves_guidance_and_rejects_invented_values(self):
        evidence = NormalizedEvidence.create(
            evidence_type="filing_delta",
            evidence_id="guidance",
            source_name="filing",
            source_timestamp=NOW,
            title="Management guidance",
            bounded_excerpt="Management expects revenue to rise 12% next year.",
        )
        claim = {
            "source_evidence_id": evidence.ref,
            "subject": "Management",
            "predicate": "expects revenue to rise",
            "object_value": "12%",
            "unit": "percent",
            "period": "next year",
            "geography": None,
            "direction": "increase",
            "claim_kind": "company_guidance",
            "source_span": "Management expects revenue to rise 12% next year.",
            "confidence": 0.95,
            "entities": [],
        }
        drafts = validate_claim_output(
            {"abstained": False, "claims": [claim, dict(claim)]}, [evidence]
        )
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].claim_kind, "company_guidance")
        invented = dict(claim, object_value="19%")
        with self.assertRaisesRegex(ValueError, "absent from exact source span"):
            validate_claim_output({"abstained": False, "claims": [invented]}, [evidence])
        unknown = dict(claim, source_evidence_id="filing_delta:invented")
        with self.assertRaisesRegex(ValueError, "unknown evidence id"):
            validate_claim_output({"abstained": False, "claims": [unknown]}, [evidence])


    def test_official_document_adapter_preserves_source_owned_provenance(self):
        row = {
            "document_id": "fed-speech",
            "source": "central_banks",
            "institution": "fed",
            "document_type": "speech",
            "title": "Policy remains data dependent",
            "published_at": NOW - timedelta(hours=2),
            "url": "https://www.federalreserve.gov/speech",
            "content": "The policy stance remains data dependent.",
            "metadata": {"feed": "official"},
            "created_at": NOW - timedelta(hours=1),
        }
        session = MagicMock()
        session.execute.return_value = Result(rows=[row])

        evidence = OfficialDocumentAdapter().collect(
            session, since=NOW - timedelta(days=1), until=NOW, limit=3
        )[0]

        self.assertEqual(evidence.evidence_type, "official_document")
        self.assertEqual(evidence.source_name, "fed")
        self.assertEqual(evidence.source_reference, row["url"])
        self.assertEqual(evidence.provenance["source"], "central_banks")
        self.assertEqual(evidence.available_at, row["created_at"])
        statement, params = session.execute.call_args.args
        self.assertIn("created_at <= :until", str(statement))
        self.assertEqual(params["until"], NOW)
        self.assertIn(
            ("macro_region", "us"),
            {(entity.entity_type, entity.normalized_key) for entity in evidence.entities},
        )


    def test_release_and_reaction_adapters_preserve_deterministic_semantics(self):
        release_row = {
            "id": "release-card",
            "release_identity": "us-cpi",
            "series_id": "CPI",
            "event_name": "Consumer prices",
            "actual": 3.1,
            "consensus": 2.9,
            "previous": 2.8,
            "revised_previous": 2.7,
            "absolute_surprise": 0.2,
            "standardized_surprise": 1.1,
            "impact": "high",
            "source": "economic_calendar",
            "observed_at": NOW,
            "released_at": NOW,
            "revision_at": None,
            "quality_flags": [],
            "stage": "final",
            "reaction_summary": {"status": "confirmed"},
            "created_at": NOW,
        }
        release_session = MagicMock()
        release_session.execute.return_value = Result(rows=[release_row])
        release = MacroReleaseAdapter().collect(
            release_session, since=NOW - timedelta(days=1), limit=7
        )[0]
        self.assertEqual(release.structured_fields["actual"], 3.1)
        self.assertEqual(release.structured_fields["consensus"], 2.9)
        self.assertEqual(release.structured_fields["previous"], 2.8)
        self.assertEqual(
            release.structured_fields["reaction_summary"], {"status": "confirmed"}
        )
        self.assertTrue(release.provenance["immutable_card"])
        statement, params = release_session.execute.call_args.args
        self.assertIn("macro_release_cards", str(statement))
        self.assertNotIn("macro_release_cards_current", str(statement))
        self.assertIn(":until", str(statement))
        self.assertIsNone(params["until"])
        self.assertEqual(params["limit"], 7)

        story_row = {
            "id": "story-window",
            "cluster_id": "cluster",
            "source_event_id": "event",
            "market_symbol": "DXY",
            "headline_at": NOW,
            "observed_at": NOW,
            "pre_headline_move": None,
            "move_5m": None,
            "move_30m": None,
            "move_session": None,
            "flags": [],
            "missing_reasons": {"move_5m": "missing_data"},
            "provenance": {"calculation": "deterministic"},
            "created_at": NOW,
        }
        release_window = {
            "id": "release-window",
            "event_id": "event",
            "instrument_symbol": "DXY",
            "horizon": "30m",
            "event_at": NOW,
            "target_at": NOW,
            "observed_at": None,
            "baseline_price": 100.0,
            "target_price": None,
            "absolute_move": None,
            "percentage_move": None,
            "volatility_adjusted_move": None,
            "expected_direction": "up",
            "sensitivity": 0.5,
            "direction_vs_expected": "unknown",
            "reaction_state": "pending",
            "missing_data_reason": "target_missing",
            "provenance": {"calculation": "deterministic"},
            "created_at": NOW,
        }
        reaction_session = MagicMock()
        reaction_session.execute.side_effect = [
            Result(rows=[story_row]),
            Result(rows=[release_window]),
        ]
        confirmations = MarketConfirmationAdapter().collect(
            reaction_session, since=NOW - timedelta(days=1), limit=4
        )
        self.assertEqual(len(confirmations), 2)
        self.assertEqual(
            confirmations[0].structured_fields["missing_reasons"],
            {"move_5m": "missing_data"},
        )
        self.assertEqual(
            confirmations[1].structured_fields["missing_data_reason"],
            "target_missing",
        )
        self.assertEqual(
            confirmations[1].structured_fields["reaction_state"], "pending"
        )
        self.assertIn(
            "confirmation may be absent", confirmations[1].bounded_excerpt.casefold()
        )


class CandidateGraphAndLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (
            cls.raw,
            cls.evidence,
            cls.group,
            cls.pattern,
            cls.edges,
            cls.capture,
            cls.adversarial,
            cls.deliverable,
        ) = load_scenario("research_data_centre_chain.json")

    def test_candidate_blocking_requires_source_diversity_and_dedupes(self):
        self.assertEqual(len(self.group.evidence), 4)
        self.assertEqual(len(self.group.source_names), 4)
        duplicated = (*self.evidence, self.evidence[0])
        groups = build_candidate_groups(duplicated, settings(), maximum_groups=30)
        self.assertTrue(groups)
        self.assertTrue(all(len({item.ref for item in group.evidence}) == len(group.evidence) for group in groups))
        one_source = tuple(
            NormalizedEvidence.create(
                **{
                    **item.to_dict(),
                    "source_name": "one source",
                    "entities": item.entities,
                }
            )
            for item in self.evidence
        )
        self.assertEqual(build_candidate_groups(one_source, settings()), ())

    def test_candidate_must_be_an_economic_proposition_not_a_topic_label(self):
        topic = strict_pattern(dict(self.raw["pattern"]))
        topic["label"] = "Semiconductor industry discussion"
        topic["definition"] = "Several semiconductor companies are discussed."
        topic["what_changed"] = "More semiconductor technology reports appeared."
        with self.assertRaisesRegex(ValueError, "not an economic proposition"):
            validate_pattern_output(topic, self.group)

    def test_candidate_blocking_does_not_count_derived_claim_as_new_source(self):
        parent = NormalizedEvidence.create(
            evidence_type="story_cluster",
            evidence_id="parent-story",
            source_name="wire",
            source_timestamp=NOW,
            title="Grid capacity bottleneck",
            bounded_excerpt="Transformer capacity constrains grid deliveries.",
        )
        derived = NormalizedEvidence.create(
            evidence_type="source_claim",
            evidence_id="derived-claim",
            source_name="claim-adapter",
            source_timestamp=NOW,
            title="Grid capacity bottleneck",
            bounded_excerpt="Transformer capacity constrains grid deliveries.",
            structured_fields={"source_evidence_id": parent.ref},
        )
        corroborating = NormalizedEvidence.create(
            evidence_type="filing_delta",
            evidence_id="corroborating-filing",
            source_name="filing",
            source_timestamp=NOW,
            title="Grid capacity bottleneck",
            bounded_excerpt="Transformer capacity constrains grid deliveries.",
        )

        self.assertEqual(
            build_candidate_groups(
                (parent, derived, corroborating), settings(), maximum_groups=30
            ),
            (),
        )

    def test_story_block_coalesces_reactions_before_term_candidates(self):
        story = NormalizedEvidence.create(
            evidence_type="story_cluster",
            evidence_id="shared-story",
            source_name="canonical-story",
            source_timestamp=NOW,
            title="Hyperscaler debt expansion",
            bounded_excerpt="Hyperscaler debt expansion is funding infrastructure.",
        )
        reactions = tuple(
            NormalizedEvidence.create(
                evidence_type="market_confirmation",
                evidence_id=f"reaction-{index}",
                source_name="market-data",
                source_timestamp=NOW,
                title="Hyperscaler debt expansion",
                bounded_excerpt="Hyperscaler debt expansion reaction window.",
                structured_fields={"cluster_id": "shared-story"},
            )
            for index in range(3)
        )

        groups = build_candidate_groups(
            (story, *reactions), settings(), maximum_groups=30
        )

        self.assertEqual(groups[0].blocking_key, "story:shared-story")
        self.assertFalse(
            any(group.blocking_key == "term:hyperscaler" for group in groups)
        )

    def test_candidate_blocking_excludes_document_boilerplate_terms(self):
        packet = tuple(
            NormalizedEvidence.create(
                evidence_type="story_cluster",
                evidence_id=f"specific-{index}",
                source_name=f"source-{index}",
                source_timestamp=NOW,
                title="Annual company earnings report",
                bounded_excerpt=(
                    "Transformer capacity bottleneck is constraining grid deliveries."
                ),
            )
            for index in range(3)
        )

        keys = {
            group.blocking_key
            for group in build_candidate_groups(packet, settings(), maximum_groups=30)
        }

        self.assertFalse(
            keys
            & {
                "term:annual",
                "term:company",
                "term:earnings",
                "term:report",
            }
        )
        self.assertTrue(any(key.startswith("phrase:") for key in keys))
        self.assertFalse(any("annual-company" in key for key in keys))

    def test_graph_deduplicates_edges_drops_cycles_and_bounds_traversal(self):
        raw_edges = json.loads(json.dumps(self.raw["edges"]))
        duplicate = dict(raw_edges[0])
        duplicate["epistemic_state"] = "hypothesis"
        duplicate["evidence_ids"] = []
        duplicate["confidence"] = None
        duplicate["valid_from"] = None
        duplicate["valid_to"] = None
        reverse = {
            "from_entity": raw_edges[-1]["to_entity"],
            "relationship": "depends_on",
            "to_entity": raw_edges[0]["from_entity"],
            "mechanism": "A reverse dependency is only a hypothesis.",
            "epistemic_state": "hypothesis",
            "evidence_ids": [],
            "confidence": None,
            "missing_evidence": ["Direct evidence"],
            "break_conditions": ["The dependency does not exist"],
            "depth": 5,
            "valid_from": None,
            "valid_to": None,
        }
        output = strict_edges(raw_edges)
        output["edges"].extend((duplicate, reverse))
        validated = validate_causal_output(
            output, self.evidence, settings(), seed_entities=self.pattern.entities
        )
        self.assertEqual(len(validated), len(self.raw["edges"]))
        paths = bounded_traversal(
            validated, "concept", "ai-demand", max_depth=3, hard_max_depth=5
        )
        self.assertTrue(paths)
        self.assertLessEqual(max(len(path) for path in paths), 3)
        with self.assertRaises(ValueError):
            bounded_traversal(validated, "concept", "ai-demand", max_depth=6, hard_max_depth=5)

    def test_lifecycle_is_deterministic_and_requires_complete_research(self):
        ready = CaseStats(
            evidence_count=6,
            source_diversity=2,
            persistence_days=10,
            snapshot_count=1,
            has_causal_chain=True,
            has_value_capture=True,
            has_adversarial_review=True,
            has_deliverable=True,
            last_evidence_at=NOW,
        )
        self.assertEqual(next_lifecycle_state("candidate", ready, settings(), now=NOW).value, "research_ready")
        incomplete = replace(ready, has_adversarial_review=False)
        self.assertEqual(next_lifecycle_state("candidate", incomplete, settings(), now=NOW).value, "corroborated")
        mature = replace(ready, evidence_count=10, snapshot_count=3)
        self.assertEqual(next_lifecycle_state("research_ready", mature, settings(), now=NOW).value, "mature")
        stale = replace(ready, last_evidence_at=NOW - timedelta(days=50))
        self.assertEqual(next_lifecycle_state("mature", stale, settings(), now=NOW).value, "weakening")
        archived = replace(ready, last_evidence_at=NOW - timedelta(days=130))
        self.assertEqual(next_lifecycle_state("weakening", archived, settings(), now=NOW).value, "archived")

    def test_scheduled_lifecycle_refresh_versions_inactive_cases_without_model_input(self):
        rows = [
            {
                "id": CASE_ID,
                "lifecycle_state": "research_ready",
                "first_seen_at": NOW - timedelta(days=90),
                "last_evidence_at": NOW - timedelta(days=50),
                "input_fingerprint": "a" * 64,
                "evidence_count": 8,
                "source_diversity": 3,
                "snapshot_count": 2,
                "has_causal_chain": True,
                "has_value_capture": True,
                "has_adversarial_review": True,
                "has_deliverable": True,
                "current_payload": {
                    "pipeline_complete": True,
                    "deliverable": {"what_changed": {"text": "Prior evidence"}},
                },
            },
            {
                "id": THEME_ID,
                "lifecycle_state": "weakening",
                "first_seen_at": NOW - timedelta(days=200),
                "last_evidence_at": NOW - timedelta(days=130),
                "input_fingerprint": "b" * 64,
                "evidence_count": 8,
                "source_diversity": 3,
                "snapshot_count": 3,
                "has_causal_chain": True,
                "has_value_capture": True,
                "has_adversarial_review": True,
                "has_deliverable": True,
                "current_payload": {"pipeline_complete": True, "deliverable": {}},
            },
        ]
        session = MagicMock()
        session.execute.return_value = Result(rows=rows)
        with patch(
            "research_intelligence.repository.publish_case_snapshot",
            side_effect=[
                SimpleNamespace(changed=True, version=3),
                SimpleNamespace(changed=True, version=4),
            ],
        ) as publish:
            transitions = refresh_case_lifecycles(
                session,
                settings(),
                correlation_id=None,
                now=NOW,
                limit=25,
            )

        self.assertEqual(
            [(item["from"], item["to"]) for item in transitions],
            [("research_ready", "weakening"), ("weakening", "archived")],
        )
        self.assertEqual(publish.call_count, 2)
        first = publish.call_args_list[0].kwargs
        self.assertEqual(first["lifecycle_state"], "weakening")
        self.assertTrue(first["payload"]["pipeline_complete"])
        self.assertEqual(
            first["payload"]["lifecycle_transition"]["reason"],
            "configured_evidence_and_inactivity_thresholds",
        )
        self.assertEqual(
            first["provenance"].prompt_version, "deterministic_lifecycle_v1"
        )
        statement, params = session.execute.call_args.args
        self.assertIn("WHERE c.lifecycle_state <> 'archived'", str(statement))
        self.assertEqual(params["limit"], 25)

    def test_scheduled_lifecycle_cannot_promote_an_unresolved_hypothesis(self):
        session = MagicMock()
        session.execute.return_value = Result(
            rows=[
                {
                    "id": CASE_ID,
                    "lifecycle_state": "corroborated",
                    "first_seen_at": NOW - timedelta(days=60),
                    "last_evidence_at": NOW,
                    "input_fingerprint": "c" * 64,
                    "evidence_count": 10,
                    "source_diversity": 4,
                    "snapshot_count": 3,
                    "has_causal_chain": True,
                    "has_value_capture": True,
                    "has_adversarial_review": True,
                    "has_deliverable": True,
                    "has_unresolved_hypothesis": True,
                    "current_payload": {"pipeline_complete": True},
                }
            ]
        )
        with patch(
            "research_intelligence.repository.publish_case_snapshot"
        ) as publish:
            transitions = refresh_case_lifecycles(
                session, settings(), now=NOW
            )
        self.assertEqual(transitions, [])
        publish.assert_not_called()
        self.assertIn(
            "JSONB_TYPEOF(sd.payload->'deliverable') = 'object'",
            str(session.execute.call_args.args[0]),
        )

    def test_all_current_hypotheses_remain_material_until_edge_state_changes(self):
        session = MagicMock()
        session.execute.return_value = Result(first={"count": 2})
        self.assertEqual(
            unresolved_material_hypotheses(session, str(CASE_ID)), 2
        )
        statement = str(session.execute.call_args.args[0])
        self.assertIn("e.epistemic_state = 'hypothesis'", statement)
        self.assertNotIn("research_data_requests", statement)

    def test_case_merge_is_idempotent_and_uses_same_identity(self):
        session = CaseSession()
        provenance = ModelProvenance(
            model_slug="fixture/model",
            prompt_version="research_pattern_discovery_v1",
            input_fingerprint=self.group.input_fingerprint,
        )
        first = upsert_case(
            session,
            self.pattern,
            self.evidence,
            evidence_input_fingerprint=self.group.input_fingerprint,
            provenance=provenance,
            correlation_id="fixture-run",
            now=NOW,
        )
        second = upsert_case(
            session,
            self.pattern,
            self.evidence,
            evidence_input_fingerprint=self.group.input_fingerprint,
            provenance=provenance,
            correlation_id="fixture-run",
            now=NOW,
        )
        self.assertEqual(first.case_id, second.case_id)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertFalse(second.changed)
        session.commit.assert_not_called()
        session.rollback.assert_not_called()
        session.close.assert_not_called()

    def test_existing_manual_theme_is_extended_not_duplicated(self):
        case = {
            "id": CASE_ID,
            "title": "Data-centre power infrastructure constraint",
            "definition": "Power infrastructure constrains data-centre construction.",
            "horizon": "multi_year",
            "lifecycle_state": "research_ready",
            "current_version": 2,
            "input_fingerprint": "a" * 64,
            "payload": {"macro_drivers": ["power capacity"]},
        }
        theme = {
            "id": THEME_ID,
            "name": "Data centre power infrastructure constraints",
            "origin": "manual",
            "source_case_id": None,
        }
        session = MagicMock()
        session.execute.side_effect = [
            Result(first=case),
            Result(first=None),
            Result(rows=[theme]),
            Result(rowcount=1),
            Result(rows=[]),
        ]
        result = promote_case_to_theme(session, str(CASE_ID), similarity_threshold=0.5)
        self.assertEqual(result, {"theme_id": str(THEME_ID), "created": False, "matched": True})
        sql = "\n".join(str(call.args[0]) for call in session.execute.call_args_list)
        self.assertIn("UPDATE investment_themes", sql)
        self.assertNotIn("INSERT INTO investment_themes", sql)
        session.commit.assert_not_called()


class ModelRunnerAndConfigTests(unittest.TestCase):
    @staticmethod
    def _runner_config(**overrides):
        research = {
            "enabled": True,
            "model_budget_usd_per_run": 0.25,
            "stages": {"pattern_discovery": True},
        }
        research.update(overrides)
        return {"research_intelligence": research}

    def test_model_stage_repairs_once_records_both_attempts_and_then_obeys_budget(self):
        responses = [
            {
                "content": "not-json",
                "model": "fixture-model",
                "tokens_input": 10,
                "tokens_output": 2,
                "cost_usd": 0.1,
                "duration_ms": 4,
            },
            {
                "content": '{"ok": true}',
                "model": "fixture-model",
                "tokens_input": 12,
                "tokens_output": 3,
                "cost_usd": 0.15,
                "duration_ms": 5,
            },
        ]

        class FakeStage:
            prompts = []

            def __init__(self, *args, **kwargs):
                self.policy = SimpleNamespace(model="fixture-model")

            def call(self, prompt):
                self.prompts.append(prompt)
                return responses.pop(0)

        session = MagicMock()
        runner = ResearchModelRunner(
            self._runner_config(), correlation_id=str(CASE_ID), session=session
        )

        def validator(value):
            if value != {"ok": True}:
                raise ValueError("wrong shape")
            return value

        with (
            patch("research_intelligence.models.LLMStage", FakeStage),
            patch(
                "research_intelligence.models.load_prompt_template",
                return_value=(
                    "Evidence {{input_json}}; relations {{relationship_vocabulary}}",
                    {"path": "fixture", "version": "v1"},
                ),
            ),
        ):
            result = runner.run(
                "pattern_discovery",
                {"evidence": ["story_cluster:one"]},
                validator,
                input_fingerprint="a" * 64,
            )
            with self.assertRaisesRegex(
                ResearchRunBudgetExceeded, "budget exhausted"
            ):
                runner.run("pattern_discovery", {}, validator)

        self.assertEqual(result.value, {"ok": True})
        self.assertAlmostEqual(result.cost_usd, 0.25)
        self.assertEqual(result.tokens_input, 22)
        self.assertEqual(result.tokens_output, 5)
        self.assertEqual(result.duration_ms, 9)
        self.assertEqual(result.provenance.input_fingerprint, "a" * 64)
        self.assertEqual(len(FakeStage.prompts), 2)
        self.assertIn("Repair the JSON once", FakeStage.prompts[1])
        attempt_params = [
            call.args[1] for call in session.execute.call_args_list
        ]
        self.assertEqual(
            [params["status"] for params in attempt_params],
            ["validation_failed", "validated"],
        )
        self.assertEqual(
            [params["attempt_number"] for params in attempt_params], [1, 2]
        )

    def test_second_invalid_model_output_fails_closed_without_persistence_value(self):
        class InvalidStage:
            def __init__(self, *args, **kwargs):
                self.policy = SimpleNamespace(model="fixture-model")

            def call(self, prompt):
                return {
                    "content": "{}",
                    "model": "fixture-model",
                    "cost_usd": 0,
                }

        session = MagicMock()
        runner = ResearchModelRunner(
            self._runner_config(model_budget_usd_per_run=1),
            correlation_id=None,
            session=session,
        )

        def reject(_value):
            raise ValueError("required evidence missing")

        with (
            patch("research_intelligence.models.LLMStage", InvalidStage),
            patch(
                "research_intelligence.models.load_prompt_template",
                return_value=("Input {{input_json}}", {"version": "v1"}),
            ),
            self.assertRaisesRegex(
                ResearchModelValidationError, "validation failed"
            ),
        ):
            runner.run("pattern_discovery", {}, reject)

        attempt_params = [
            call.args[1] for call in session.execute.call_args_list
        ]
        self.assertEqual(len(attempt_params), 2)
        self.assertTrue(
            all(params["status"] == "validation_failed" for params in attempt_params)
        )

    def test_configuration_rejects_unsafe_graph_lifecycle_and_reasoning_values(self):
        with self.assertRaisesRegex(ValueError, "graph.depth"):
            ResearchSettings.from_config(
                self._runner_config(graph={"depth": 4, "hard_depth": 3})
            )
        with self.assertRaisesRegex(ValueError, "archive_days"):
            ResearchSettings.from_config(
                self._runner_config(
                    lifecycle_thresholds={"weakening_days": 40, "archive_days": 20}
                )
            )
        with self.assertRaisesRegex(ValueError, "reasoning_effort"):
            ResearchSettings.from_config(
                self._runner_config(
                    reasoning_effort={"pattern_discovery": "unbounded"}
                )
            )


    def test_configuration_bounds_macro_packet_and_driver_cardinality(self):
        parsed = ResearchSettings.from_config(
            self._runner_config(
                limits={
                    "maximum_candidate_evidence": 120,
                    "maximum_macro_evidence": 32,
                    "maximum_market_drivers": 6,
                }
            )
        )

        self.assertEqual(parsed.maximum_macro_evidence, 32)
        self.assertEqual(parsed.maximum_market_drivers, 6)

    def test_expansive_stages_use_bounded_v2_contracts(self):
        parsed = ResearchSettings.from_config(self._runner_config())

        self.assertEqual(STAGE_VERSIONS["claim_extraction"], "research_claim_extraction_v2")
        self.assertEqual(STAGE_VERSIONS["pattern_discovery"], "research_pattern_discovery_v2")
        self.assertEqual(STAGE_VERSIONS["causal_chain"], "research_causal_chain_v2")
        self.assertEqual(STAGE_VERSIONS["value_capture"], "research_value_capture_v2")
        self.assertEqual(STAGE_VERSIONS["adversarial"], "research_adversarial_v2")
        self.assertEqual(STAGE_VERSIONS["deliverable"], "research_deliverable_v2")
        self.assertEqual(STAGE_VERSIONS["macro_transmission"], "macro_transmission_v3")
        self.assertTrue(
            parsed.prompt_templates["value_capture"].endswith(
                "research_value_capture_v2.txt"
            )
        )
        self.assertEqual(parsed.stage_max_output_tokens["adversarial"], 4096)
        self.assertEqual(
            STAGE_SCHEMAS["value_capture"]["schema"]["properties"]["assessments"][
                "maxItems"
            ],
            3,
        )
        self.assertEqual(
            STAGE_SCHEMAS["adversarial"]["schema"]["properties"][
                "counterevidence"
            ]["maxItems"],
            5,
        )
        self.assertEqual(
            STAGE_SCHEMAS["claim_extraction"]["schema"]["properties"]["claims"][
                "maxItems"
            ],
            4,
        )
        self.assertEqual(
            STAGE_SCHEMAS["causal_chain"]["schema"]["properties"]["edges"]["maxItems"],
            12,
        )
        self.assertEqual(
            STAGE_SCHEMAS["deliverable"]["schema"]["properties"]["what_to_watch"][
                "maxItems"
            ],
            6,
        )


class ScenarioEndToEndTests(unittest.TestCase):
    def _assert_scenario(self, filename, expected_label, expected_capture_key):
        raw, evidence, group, pattern, edges, capture, adversarial, deliverable = load_scenario(filename)
        self.assertEqual(pattern.label, expected_label)
        self.assertEqual(len(group.evidence), len(evidence))
        self.assertTrue(any(edge.epistemic_state == "observed" for edge in edges))
        self.assertTrue(any(edge.epistemic_state == "hypothesis" for edge in edges))
        self.assertEqual(capture[0].node_key, expected_capture_key)
        self.assertIsNone(capture[0].dimensions["valuation"])
        self.assertTrue(capture[0].unknowns)
        self.assertTrue(adversarial.counterevidence)
        self.assertTrue(adversarial.data_requests)
        self.assertEqual(adversarial.data_requests[0].priority, "high")
        self.assertTrue(deliverable.what_changed.evidence_ids)
        self.assertTrue(deliverable.weak_links_unknowns)
        existing = [{"id": str(CASE_ID), "semantic_fingerprint": pattern.semantic_fingerprint, "title": pattern.label, "aliases": []}]
        self.assertEqual(select_case_match(pattern, existing, 0.99)["id"], str(CASE_ID))
        prose = json.dumps(deliverable.to_dict()).casefold()
        self.assertNotIn("buy ", prose)
        self.assertNotIn("sell ", prose)
        self.assertNotIn("position size", prose)
        return raw

    def test_beef_chain_fixture_end_to_end(self):
        raw = self._assert_scenario(
            "research_beef_chain.json", "Beef supply constraint", "meat-processors"
        )
        self.assertIn("Distributor beef purchase contracts", json.dumps(raw["adversarial"]))

    def test_data_centre_chain_fixture_end_to_end(self):
        raw = self._assert_scenario(
            "research_data_centre_chain.json",
            "Data-centre power infrastructure constraint",
            "transformer-equipment",
        )
        self.assertIn("Transformer equipment lead times", json.dumps(raw["adversarial"]))

    def test_counterevidence_and_requests_persist_with_conflict_guards(self):
        _, evidence, _, _, edges, _, adversarial, _ = load_scenario(
            "research_data_centre_chain.json"
        )
        known_edges = {fingerprint: f"edge-{index}" for index, fingerprint in enumerate(edge_fingerprints(edges))}
        seen_counters = {}
        calls = []

        def execute(statement, params=None):
            sql = str(statement)
            params = params or {}
            calls.append((sql, params))
            if "SELECT id FROM research_causal_edges" in sql:
                return Result(first={"id": known_edges[params["fingerprint"]]})
            if "INSERT INTO research_counterevidence (" in sql:
                fingerprint = params["counter_fingerprint"]
                if fingerprint in seen_counters:
                    return Result(first=None)
                seen_counters[fingerprint] = f"counter-{len(seen_counters)}"
                return Result(first={"id": seen_counters[fingerprint]})
            if "SELECT id FROM research_counterevidence" in sql:
                return Result(first={"id": seen_counters[params["fingerprint"]]})
            if "INSERT INTO research_data_requests" in sql:
                return Result(rowcount=1)
            return Result(rowcount=1)

        session = MagicMock()
        session.execute.side_effect = execute
        provenance = ModelProvenance(
            model_slug="fixture/model",
            prompt_version="research_adversarial_v1",
            input_fingerprint="b" * 64,
        )
        first = persist_adversarial(session, str(CASE_ID), adversarial, evidence, provenance)
        second = persist_adversarial(session, str(CASE_ID), adversarial, evidence, provenance)
        self.assertEqual(first["counterevidence"], len(adversarial.counterevidence))
        self.assertEqual(second["counterevidence"], len(adversarial.counterevidence))
        self.assertEqual(len(seen_counters), len(adversarial.counterevidence))
        sql = "\n".join(item[0] for item in calls)
        self.assertIn("ON CONFLICT (case_id, counter_fingerprint) DO NOTHING", sql)
        self.assertIn("ON CONFLICT (case_id, request_fingerprint) DO UPDATE", sql)
        session.commit.assert_not_called()


class DeployedResearchEntryPointTests(unittest.TestCase):
    CONFIG = {
        "research_intelligence": {
            "enabled": True,
            "macro_drivers_enabled": True,
            "claim_extraction_enabled": False,
            "limits": {"maximum_candidate_evidence": 17},
        }
    }

    def test_empty_normalized_window_returns_bounded_states_without_model_work(self):
        collection = EvidenceCollection(items=(), failures={})
        session = MagicMock()
        case = {"case": {"title": "Existing case"}, "entities": [], "evidence": []}

        with (
            patch(
                "research_intelligence.service.EvidenceRegistry"
            ) as registry_type,
            patch(
                "research_intelligence.service.refresh_case_lifecycles",
                return_value=[],
            ),
            patch(
                "research_intelligence.service.find_case_match_rows",
                return_value=[],
            ),
            patch("research_intelligence.service.get_case", return_value=case),
        ):
            registry_type.return_value.collect.return_value = collection

            discovery = run_discovery(session, self.CONFIG)
            update = run_case_update(session, self.CONFIG, str(CASE_ID))
            macro = run_macro_transmission(session, self.CONFIG)

        self.assertEqual(discovery["status"], "completed")
        self.assertEqual(discovery["evidence_count"], 0)
        self.assertEqual(update, {"status": "no_evidence", "case_id": str(CASE_ID)})
        self.assertEqual(macro["status"], "no_evidence")
        self.assertEqual(
            [call.kwargs["limit"] for call in registry_type.return_value.collect.call_args_list],
            [17, 17, 17],
        )


class MarketDriverAndMalformedOutputTests(unittest.TestCase):
    def setUp(self):
        self.evidence = (
            NormalizedEvidence.create(
                evidence_type="macro_observation",
                evidence_id="rates",
                source_name="official",
                source_timestamp=NOW,
                title="Relative policy expectations support the dollar",
                bounded_excerpt="Relative policy expectations support the dollar while inflation pressure eases.",
                entities=(normalize_entity("market", "DXY"),),
                structured_fields={"latest": 2.71, "units": "Percent"},
            ),
        )
        self.driver = {
            "target": "DXY",
            "driver_key": "relative_policy_expectations",
            "driver_label": "Relative policy expectations",
            "direction": "supportive",
            "strength": "high",
            "horizon": "weeks",
            "mechanism": "Relative policy expectations support the dollar.",
            "evidence_ids": ["macro_observation:rates"],
            "invalidation_conditions": ["Relative policy expectations converge"],
            "confidence": 0.8,
            "confidence_rationale": "The supplied official observation directly supports the mechanism.",
        }

    def test_market_driver_is_evidence_linked_bounded_and_change_aware(self):
        first = validate_market_driver_output(
            {"abstained": False, "drivers": [self.driver]}, self.evidence, ["DXY"]
        )
        self.assertTrue(first[0].changed_since_prior)
        supplied_number = dict(
            self.driver,
            mechanism="The supplied high-yield spread is 2.71%.",
        )
        self.assertEqual(
            len(
                validate_market_driver_output(
                    {"abstained": False, "drivers": [supplied_number]},
                    self.evidence,
                    ["DXY"],
                )
            ),
            1,
        )
        with self.assertRaisesRegex(ValueError, "unsupported numeric"):
            validate_market_driver_output(
                {
                    "abstained": False,
                    "drivers": [
                        dict(self.driver, mechanism="The spread is 9.99%.")
                    ],
                },
                self.evidence,
                ["DXY"],
            )
        prior = [{**self.driver, "evidence_ids": self.driver["evidence_ids"]}]
        repeated = validate_market_driver_output(
            {"abstained": False, "drivers": [self.driver]},
            self.evidence,
            ["DXY"],
            prior_drivers=prior,
        )

        self.assertFalse(repeated[0].changed_since_prior)
        changed = dict(self.driver, direction="headwind")
        self.assertTrue(
            validate_market_driver_output(
                {"abstained": False, "drivers": [changed]},
                self.evidence,
                ["DXY"],
                prior_drivers=prior,
            )[0].changed_since_prior
        )
        changed_horizon = dict(self.driver, horizon="months")
        self.assertTrue(
            validate_market_driver_output(
                {"abstained": False, "drivers": [changed_horizon]},
                self.evidence,
                ["DXY"],
                prior_drivers=prior,
            )[0].changed_since_prior
        )
        with self.assertRaisesRegex(ValueError, "not configured"):
            validate_market_driver_output(
                {"abstained": False, "drivers": [dict(self.driver, target="UNKNOWN")]},
                self.evidence,
                ["DXY"],
            )
        with self.assertRaisesRegex(ValueError, "unknown evidence id"):
            validate_market_driver_output(
                {"abstained": False, "drivers": [dict(self.driver, evidence_ids=["macro_observation:invented"])]},
                self.evidence,
                ["DXY"],
            )
        with self.assertRaisesRegex(ValueError, "dedicated fields"):
            validate_market_driver_output(
                {
                    "abstained": False,
                    "drivers": [
                        dict(
                            self.driver,
                            mechanism=(
                                "Relative policy expectations support the dollar "
                                "[macro_observation:rates]."
                            ),
                        )
                    ],
                },
                self.evidence,
                ["DXY"],
            )
        with self.assertRaisesRegex(ValueError, "count exceeds"):
            validate_market_driver_output(
                {
                    "abstained": False,
                    "drivers": [
                        dict(self.driver, driver_key=f"bounded_driver_{index}")
                        for index in range(25)
                    ],
                },
                self.evidence,
                ["DXY"],
            )


    def test_shared_factor_state_projects_to_bounded_market_transmissions(self):
        factor = {
            "factor_key": "relative_policy_expectations",
            "factor_label": "Relative policy expectations",
            "state": "rising",
            "strength": "high",
            "horizon": "weeks",
            "mechanism": "Policy expectations diverge across major economies.",
            "evidence_ids": ["macro_observation:rates"],
            "confidence": 0.8,
            "confidence_rationale": "The supplied official observation supports the factor state.",
            "invalidation_conditions": ["Relative policy expectations converge"],
            "transmissions": [
                {
                    "target": "DXY",
                    "direction": "supportive",
                    "mechanism": "Relative policy expectations support the dollar.",
                    "invalidation_conditions": [],
                },
                {
                    "target": "XAUUSD",
                    "direction": "headwind",
                    "mechanism": "Relative policy expectations weigh on gold.",
                    "invalidation_conditions": ["Safe-haven demand dominates"],
                },
            ],
        }
        assessment = validate_factor_market_output(
            {"abstained": False, "factors": [factor]},
            self.evidence,
            ["DXY", "XAUUSD"],
        )
        self.assertEqual(len(assessment.factors), 1)
        self.assertEqual(len(assessment.drivers), 2)
        self.assertEqual(
            {driver.driver_key for driver in assessment.drivers},
            {"relative_policy_expectations"},
        )
        self.assertEqual(
            {driver.target for driver in assessment.drivers},
            {"DXY", "XAUUSD"},
        )

        duplicate_target = dict(
            factor,
            transmissions=[factor["transmissions"][0], factor["transmissions"][0]],
        )
        with self.assertRaisesRegex(ValueError, "duplicate target transmission"):
            validate_factor_market_output(
                {"abstained": False, "factors": [duplicate_target]},
                self.evidence,
                ["DXY", "XAUUSD"],
            )
        with self.assertRaisesRegex(ValueError, "duplicate economic factor"):
            validate_factor_market_output(
                {"abstained": False, "factors": [factor, factor]},
                self.evidence,
                ["DXY", "XAUUSD"],
            )
        with self.assertRaisesRegex(ValueError, "market driver count exceeds"):
            validate_factor_market_output(
                {
                    "abstained": False,
                    "factors": [
                        dict(
                            factor,
                            factor_key=f"factor_{index}",
                            transmissions=[factor["transmissions"][0]],
                        )
                        for index in range(2)
                    ],
                },
                self.evidence,
                ["DXY", "XAUUSD"],
                maximum_drivers=1,
            )

    def test_factor_persistence_reuses_factor_id_for_target_drivers(self):
        factor = validate_factor_market_output(
            {
                "abstained": False,
                "factors": [
                    {
                        "factor_key": "relative_policy_expectations",
                        "factor_label": "Relative policy expectations",
                        "state": "rising",
                        "strength": "high",
                        "horizon": "weeks",
                        "mechanism": "Policy expectations diverge across major economies.",
                        "evidence_ids": ["macro_observation:rates"],
                        "confidence": 0.8,
                        "confidence_rationale": "The supplied official observation supports the factor state.",
                        "invalidation_conditions": ["Relative policy expectations converge"],
                        "transmissions": [
                            {
                                "target": "DXY",
                                "direction": "supportive",
                                "mechanism": "Relative policy expectations support the dollar.",
                                "invalidation_conditions": [],
                            }
                        ],
                    }
                ],
            },
            self.evidence,
            ["DXY"],
        )
        provenance = ModelProvenance(
            model_slug="fixture/model",
            prompt_version="macro_transmission_v3",
            input_fingerprint="factor-stage-input",
        )
        session = MagicMock()
        session.execute.side_effect = [
            Result(first=None),
            Result(first={"id": "factor-1"}),
            Result(rowcount=1),
            Result(rowcount=1),
        ]
        factor_ids, changed = persist_economic_factors(
            session, factor.factors, self.evidence, provenance
        )
        self.assertEqual(factor_ids, {"relative_policy_expectations": "factor-1"})
        self.assertEqual(changed, 1)
        factor_insert = next(
            call
            for call in session.execute.call_args_list
            if "INSERT INTO research_economic_factors" in str(call.args[0])
        )
        self.assertEqual(
            factor_insert.args[1]["prompt_version"], "macro_transmission_v3"
        )
        driver_session = MagicMock()
        driver_session.execute.side_effect = [
            Result(first=None),
            Result(first={"id": "driver-1"}),
            Result(rowcount=1),
        ]
        self.assertEqual(
            persist_market_drivers(
                driver_session,
                factor.drivers,
                self.evidence,
                provenance,
                factor_ids=factor_ids,
            ),
            1,
        )
        driver_insert = next(
            call
            for call in driver_session.execute.call_args_list
            if "INSERT INTO research_market_drivers" in str(call.args[0])
        )
        self.assertEqual(driver_insert.args[1]["factor_id"], "factor-1")
        first_fingerprint = driver_insert.args[1]["input_fingerprint"]
        next_driver_session = MagicMock()
        next_driver_session.execute.side_effect = [
            Result(first={"id": "driver-1", "input_fingerprint": first_fingerprint}),
            Result(rowcount=1),
            Result(first={"id": "driver-2"}),
            Result(rowcount=1),
        ]
        self.assertEqual(
            persist_market_drivers(
                next_driver_session,
                factor.drivers,
                self.evidence,
                provenance,
                factor_ids={"relative_policy_expectations": "factor-2"},
            ),
            1,
        )
        next_insert = next(
            call
            for call in next_driver_session.execute.call_args_list
            if "INSERT INTO research_market_drivers" in str(call.args[0])
        )
        self.assertEqual(next_insert.args[1]["factor_id"], "factor-2")
        self.assertNotEqual(
            next_insert.args[1]["input_fingerprint"], first_fingerprint
        )

    def test_driver_persistence_detects_change_from_semantic_content_not_stage_input(self):
        draft = validate_market_driver_output(
            {"abstained": False, "drivers": [self.driver]},
            self.evidence,
            ["DXY"],
        )[0]
        provenance = ModelProvenance(
            model_slug="fixture/model",
            prompt_version="macro_transmission_v1",
            input_fingerprint="stage-input",
        )
        first_session = MagicMock()
        first_session.execute.side_effect = [
            Result(first=None),
            Result(first={"id": "driver-1"}),
            Result(rowcount=1),
        ]
        self.assertEqual(
            persist_market_drivers(
                first_session, [draft], self.evidence, provenance
            ),
            1,
        )
        insert_call = next(
            call
            for call in first_session.execute.call_args_list
            if "INSERT INTO research_market_drivers" in str(call.args[0])
        )
        semantic_fingerprint = insert_call.args[1]["input_fingerprint"]
        self.assertNotEqual(semantic_fingerprint, "stage-input")
        self.assertTrue(insert_call.args[1]["changed_since_prior"])

        repeated_session = MagicMock()
        repeated_session.execute.return_value = Result(
            first={"id": "driver-1", "input_fingerprint": semantic_fingerprint}
        )
        self.assertEqual(
            persist_market_drivers(
                repeated_session, [draft], self.evidence, provenance
            ),
            0,
        )
        self.assertEqual(repeated_session.execute.call_count, 1)

        changed_session = MagicMock()
        changed_session.execute.side_effect = [
            Result(
                first={
                    "id": "driver-1",
                    "input_fingerprint": semantic_fingerprint,
                }
            ),
            Result(rowcount=1),
            Result(first={"id": "driver-2"}),
            Result(rowcount=1),
        ]
        changed_draft = replace(
            draft,
            direction="headwind",
            mechanism="Relative policy expectations now weigh on the dollar.",
        )
        self.assertEqual(
            persist_market_drivers(
                changed_session, [changed_draft], self.evidence, provenance
            ),
            1,
        )
        sql = "\n".join(
            str(call.args[0]) for call in changed_session.execute.call_args_list
        )
        self.assertIn("SET superseded_at", sql)

    def test_malformed_outputs_fail_closed_without_fabricated_fallbacks(self):
        raw, evidence, group, pattern, edges, capture, _, _ = load_scenario(
            "research_data_centre_chain.json"
        )
        malformed_pattern = strict_pattern(dict(raw["pattern"]))
        malformed_pattern["supporting_evidence_ids"] = ["story_cluster:invented"]
        with self.assertRaisesRegex(ValueError, "unknown evidence id"):
            validate_pattern_output(malformed_pattern, group)
        invented_number = strict_pattern(dict(raw["pattern"]))
        invented_number["what_changed"] = "Revenue accelerated by 987%."
        with self.assertRaisesRegex(ValueError, "unsupported numeric"):
            validate_pattern_output(invented_number, group)
        recommendation = strict_pattern(dict(raw["pattern"]))
        recommendation["what_changed"] = "Buy shares now because the bottleneck persists."
        with self.assertRaisesRegex(ValueError, "advisory policy"):
            validate_pattern_output(recommendation, group)
        causal = strict_edges(raw["edges"])
        causal["edges"][0]["epistemic_state"] = "observed"
        causal["edges"][0]["evidence_ids"] = ["story_cluster:power-queues"]
        with self.assertRaisesRegex(ValueError, "direct observation"):
            validate_causal_output(causal, evidence, settings(), seed_entities=pattern.entities)
        bad_capture = strict_capture(raw["capture"])
        bad_capture["assessments"][0]["evidence_ids"] = []
        with self.assertRaisesRegex(ValueError, "require evidence"):
            validate_value_capture_output(bad_capture, evidence)
        fingerprints = edge_fingerprints(edges)
        bad_deliverable = strict_deliverable(raw["deliverable"], fingerprints)
        bad_deliverable["transmission"]["edge_fingerprints"] = ["f" * 64]
        with self.assertRaisesRegex(ValueError, "unknown causal edge"):
            validate_deliverable_output(
                bad_deliverable,
                evidence,
                edge_fingerprints=fingerprints,
                assessment_nodes=[(item.node_type, item.node_key) for item in capture],
            )

    def test_relationship_vocabulary_rejects_semantic_expansion(self):
        self.assertEqual(validate_relationship("raises_demand_for"), "raises_demand_for")
        with self.assertRaisesRegex(ValueError, "unsupported causal relationship"):
            validate_relationship("magically_causes")


if __name__ == "__main__":
    unittest.main()
