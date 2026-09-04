import json
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from research_intelligence.benchmarks import (  # noqa: E402
    list_benchmarks,
)
from research_intelligence.config import ResearchSettings  # noqa: E402
from research_intelligence.context import (  # noqa: E402
    ReplayLeakageError,
    ResearchContext,
)
from research_intelligence.contracts import (  # noqa: E402
    VALUE_CAPTURE_DIMENSIONS,
    ModelProvenance,
    NormalizedEvidence,
    canonical_fingerprint,
)
from research_intelligence.evaluation import (  # noqa: E402
    _case_corpus,
    build_benchmark_scorecard,
)
from research_intelligence.models import (  # noqa: E402
    ModelStageResult,
    ResearchModelRunner,
)
from research_intelligence.replay import (  # noqa: E402
    ReplayCaseResult,
    config_with_replay_overrides,
    execute_replay_research,
)
from research_intelligence.scorecards import (  # noqa: E402
    annotate_benchmark_scorecard,
    validate_human_annotations,
)
from research_intelligence.service import run_model_stage  # noqa: E402

from contracts.runtime_config import (  # noqa: E402
    AppConfig,
    DatabaseConfig,
    LlmConfig,
    ResearchDiscoveryConfig,
    ResearchIntelligenceConfig,
    ResearchLimitsConfig,
    ResearchStageConfig,
)

CUTOFF = datetime(2024, 6, 30, 23, 59, tzinfo=UTC)
IMPORTANCE_KEYS = (
    "economic_significance",
    "market_sensitivity",
    "persistence",
    "breadth",
    "investability",
    "evidence_strength",
    "time_sensitivity",
)


class Result:
    def __init__(self, *, first=None, rows=None, rowcount=0):
        self._first = first
        self._rows = list(rows or [])
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._rows


class PointInTimeContractTests(unittest.TestCase):
    @staticmethod
    def evidence(
        evidence_id,
        *,
        available_at=CUTOFF - timedelta(days=2),
        point_in_time_safe=True,
        valid_from=None,
        valid_to=None,
        structured_fields=None,
        provenance=None,
        evidence_type="official_document",
    ):
        return NormalizedEvidence.create(
            evidence_type=evidence_type,
            evidence_id=evidence_id,
            source_name="Synthetic official source",
            source_timestamp=CUTOFF - timedelta(days=3),
            available_at=available_at,
            availability_basis="fixture_publication_time",
            point_in_time_safe=point_in_time_safe,
            valid_from=valid_from,
            valid_to=valid_to,
            title=f"Evidence {evidence_id}",
            bounded_excerpt="A bounded source observation.",
            structured_fields=structured_fields or {},
            provenance=provenance or {},
        )

    def test_replay_filters_every_future_vintage_category_and_is_idempotent(self):
        visible = self.evidence("visible")
        future = self.evidence("future", available_at=CUTOFF + timedelta(seconds=1))
        unsafe = self.evidence("unsafe", point_in_time_safe=False)
        revision = self.evidence(
            "future-revision",
            structured_fields={"revision_at": (CUTOFF + timedelta(days=1)).isoformat()},
        )
        reaction = self.evidence(
            "future-reaction",
            evidence_type="market_confirmation",
            structured_fields={
                "target_at": (CUTOFF + timedelta(minutes=30)).isoformat(),
                "observed_at": (CUTOFF + timedelta(minutes=30)).isoformat(),
            },
        )
        later_version = self.evidence(
            "later-version", valid_from=CUTOFF + timedelta(days=1)
        )
        expired_version = self.evidence(
            "expired-version", valid_to=CUTOFF - timedelta(seconds=1)
        )
        corpus = (
            visible,
            future,
            unsafe,
            revision,
            reaction,
            later_version,
            expired_version,
        )
        context = ResearchContext.replay(CUTOFF, run_id="replay-test")

        self.assertEqual(context.filter_evidence(corpus), (visible,))
        first_audit = context.audit.to_dict()
        self.assertEqual(first_audit["evidence_considered"], 7)
        self.assertEqual(first_audit["evidence_included"], 1)
        self.assertEqual(first_audit["future_evidence_excluded"], 1)
        self.assertEqual(first_audit["integrity_exclusions"], 1)
        self.assertEqual(first_audit["future_revisions_excluded"], 1)
        self.assertEqual(first_audit["future_reaction_windows_excluded"], 1)
        self.assertEqual(first_audit["versions_excluded"], 2)

        self.assertEqual(context.filter_evidence(corpus), (visible,))
        self.assertEqual(context.audit.to_dict(), first_audit)

    def test_replay_guards_model_inputs_and_keeps_benchmark_answers_out(self):
        visible = self.evidence("visible")
        context = ResearchContext.replay(
            CUTOFF, run_id="secret-run", benchmark_id="answer-bearing-episode"
        )
        context.filter_evidence((visible,))
        context.guard_model_input(
            {
                "evidence_ids": [visible.ref],
                "available_at": visible.available_at.isoformat(),
            },
            stage="allowed",
        )
        self.assertEqual(
            context.to_prompt_metadata(),
            {
                "mode": "replay",
                "as_of": CUTOFF.isoformat(),
                "point_in_time_required": True,
            },
        )

        with self.assertRaisesRegex(ReplayLeakageError, "benchmark evaluator field"):
            context.guard_model_input(
                {"expected_developments": ["answer"]}, stage="pattern_discovery"
            )
        with self.assertRaisesRegex(ReplayLeakageError, "non-as-of evidence"):
            ResearchContext.replay(CUTOFF).guard_model_input(
                {"evidence_ids": ["official_document:not-supplied"]},
                stage="causal_chain",
            )
        with self.assertRaisesRegex(ReplayLeakageError, "future timestamp"):
            ResearchContext.replay(CUTOFF).guard_model_input(
                {"released_at": (CUTOFF + timedelta(seconds=1)).isoformat()},
                stage="deliverable",
            )

    def test_same_reference_with_changed_content_is_rechecked(self):
        visible = self.evidence("versioned")
        future_payload = visible.to_dict()
        future_payload.pop("content_fingerprint")
        future_payload.update(
            {
                "source_timestamp": CUTOFF + timedelta(days=1),
                "available_at": CUTOFF + timedelta(days=1),
                "structured_fields": {"later": True},
            }
        )
        future_same_ref = NormalizedEvidence.create(**future_payload)
        context = ResearchContext.replay(CUTOFF)
        self.assertEqual(context.filter_evidence((visible,)), (visible,))
        self.assertEqual(context.filter_evidence((future_same_ref,)), ())
        self.assertEqual(context.audit.future_evidence_excluded, 1)

    def test_benchmark_quality_does_not_self_score_from_input_evidence(self):
        case = ReplayCaseResult(
            semantic_fingerprint="a" * 64,
            title="Generic capacity change",
            definition="A generic economic proposition.",
            case_is_economic_proposition=True,
            proposition_rationale="Supplied evidence supports a change.",
            lifecycle_state="candidate",
            first_qualifying_evidence_at=CUTOFF,
            first_detection_at=CUTOFF,
            evidence_count=1,
            source_diversity=1,
            maximum_graph_depth=0,
            payload={
                "evidence": [
                    {
                        "bounded_excerpt": (
                            "evaluator-only phrase present in source input"
                        )
                    }
                ],
                "deliverable": {"what_changed": {"text": "Generic change"}},
            },
        )
        corpus = _case_corpus((case,))
        self.assertIn("Generic change", corpus)
        self.assertNotIn("evaluator-only phrase", corpus)


class ControlledStageExecutor:
    LABELS = {
        "agricultural_supply_shock": "Livestock supply constraints raise meat costs",
        "ai_infrastructure_expansion": "AI infrastructure demand creates capacity constraints",
        "monetary_policy_regime_change": "Inflation pressure raises policy expectations",
        "metaverse_marketing_noise": "Metaverse marketing discussion",
    }

    def __init__(self, episode_id):
        self.episode_id = episode_id
        self.accepted_pattern = False

    @staticmethod
    def _evidence_refs(payload):
        return [item["evidence_ref"] for item in payload.get("evidence", [])]

    @staticmethod
    def _entities(payload):
        output = []
        seen = set()
        for item in payload.get("evidence", []):
            for entity in item.get("entities", []):
                identity = (entity["entity_type"], entity["normalized_key"])
                if identity in seen:
                    continue
                seen.add(identity)
                output.append(
                    {
                        "entity_type": entity["entity_type"],
                        "name": entity["display_name"],
                    }
                )
        return output

    def _pattern(self, payload):
        refs = self._evidence_refs(payload)
        entities = self._entities(payload)
        return {
            "abstained": False,
            "coherent": True,
            "label": self.LABELS[self.episode_id],
            "definition": "A persistent supply and demand change is altering capacity and costs across the observed chain.",
            "case_type": "structural",
            "horizon": "months",
            "what_changed": "Independent evidence shows demand rising while available capacity remains constrained.",
            "supporting_evidence_ids": refs[: min(3, len(refs))],
            "contradicting_evidence_ids": [],
            "context_evidence_ids": [],
            "entities": entities,
            "industries": list(payload.get("industries", [])),
            "macro_drivers": ["Changing demand and constrained capacity"],
            "missing_information": [
                "Persistence and realized pricing power remain unknown"
            ],
            "importance": {key: "moderate" for key in IMPORTANCE_KEYS},
            "importance_rationale": {
                key: "Independent supplied evidence supports this bounded assessment."
                for key in IMPORTANCE_KEYS
            },
            "aliases": [],
        }

    def raw_output(self, stage, payload):
        if stage == "claim_extraction":
            return {"abstained": True, "claims": []}
        refs = self._evidence_refs(payload)
        if stage == "pattern_discovery":
            if self.episode_id == "metaverse_marketing_noise" or self.accepted_pattern:
                raw = self._pattern(payload)
                raw.update(
                    {
                        "abstained": True,
                        "coherent": False,
                        "supporting_evidence_ids": [],
                        "contradicting_evidence_ids": [],
                        "context_evidence_ids": [],
                    }
                )
                return raw
            self.accepted_pattern = True
            return self._pattern(payload)
        if stage == "causal_chain":
            entities = self._entities(payload)
            if len(entities) < 2:
                return {"abstained": True, "edges": []}
            return {
                "abstained": False,
                "edges": [
                    {
                        "from_entity": entities[0],
                        "relationship": "depends_on",
                        "to_entity": entities[1],
                        "mechanism": "The observed economic change depends on the supplied capacity constraint.",
                        "epistemic_state": "supported",
                        "evidence_ids": refs[:1],
                        "missing_evidence": ["Further independent confirmation"],
                        "break_conditions": ["The observed dependency weakens"],
                        "depth": 1,
                        "confidence": 0.7,
                        "valid_from": None,
                        "valid_to": None,
                    }
                ],
            }
        if stage == "value_capture":
            edge = payload["causal_edges"][0]
            dimensions = {key: None for key in VALUE_CAPTURE_DIMENSIONS}
            dimensions["evidence_strength"] = "moderate"
            rationale = {key: "" for key in VALUE_CAPTURE_DIMENSIONS}
            rationale["evidence_strength"] = (
                "The supplied source establishes exposure but not complete unit economics."
            )
            return {
                "abstained": False,
                "assessments": [
                    {
                        "node": {
                            "entity_type": edge["to_type"],
                            "name": edge["to_name"],
                        },
                        "dimensions": dimensions,
                        "rationale": rationale,
                        "evidence_ids": refs[:1],
                        "unknowns": ["Realized pricing power remains unknown"],
                    }
                ],
            }
        if stage == "adversarial":
            edge_fingerprint = payload["edge_fingerprints"][0]
            return {
                "abstained": False,
                "counterevidence": [
                    {
                        "kind": "alternative_explanation",
                        "statement": "A temporary demand shift could explain the observed change.",
                        "epistemic_state": "hypothesis",
                        "evidence_ids": [],
                        "edge_fingerprint": edge_fingerprint,
                        "rationale": "Available evidence does not yet separate persistence from a temporary shift.",
                    }
                ],
                "data_requests": [
                    {
                        "subject": "Capacity persistence",
                        "requested_evidence_type": "industry_capacity",
                        "reason": "Test the weakest dependency in the causal chain.",
                        "desired_frequency": "quarterly",
                        "priority": "high",
                        "candidate_source_class": "industry",
                    }
                ],
                "invalidation_conditions": [
                    "The observed constraint eases without downstream effects"
                ],
                "strengthening_observations": [
                    "Independent capacity evidence confirms persistence"
                ],
                "weakest_edge_fingerprint": edge_fingerprint,
            }
        if stage == "deliverable":
            edge_fingerprint = payload["edge_fingerprints"][0]
            node = payload["value_capture"][0]
            return {
                "abstained": False,
                "what_changed": {
                    "text": "Demand rose while observed capacity remained constrained.",
                    "evidence_ids": refs[:1],
                },
                "why_it_matters": {
                    "text": "The constraint can transmit through costs and industry capacity.",
                    "evidence_ids": refs[:1],
                },
                "transmission": {
                    "text": "Demand transmits through the supported capacity dependency.",
                    "edge_fingerprints": [edge_fingerprint],
                },
                "potential_capture": [
                    {
                        "node_type": node["node_type"],
                        "node_key": node["node_key"],
                        "node_name": node["node_name"],
                        "text": "Exposure exists, but retained economics remain unproven.",
                        "evidence_ids": refs[:1],
                    }
                ],
                "evidence_for": [
                    {
                        "text": "Independent evidence supports the observed change.",
                        "evidence_ids": refs[:1],
                    }
                ],
                "evidence_against": [],
                "weak_links_unknowns": ["Persistence and pricing power remain unknown"],
                "what_to_watch": [
                    "Capacity, realized pricing, and substitution evidence"
                ],
            }
        raise AssertionError(f"unexpected stage: {stage}")

    def __call__(self, stage, payload, validator, fingerprint):
        value = validator(self.raw_output(stage, payload))
        return ModelStageResult(
            value=value,
            provenance=ModelProvenance(
                model_slug="fixture/controlled",
                prompt_version=f"controlled_{stage}_v1",
                input_fingerprint=fingerprint,
                metadata={"attempt_count": 1},
            ),
            cost_usd=0.001,
            tokens_input=20,
            tokens_output=10,
            duration_ms=2,
        )


class ControlledBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = ResearchSettings.from_config(
            ResearchIntelligenceConfig(
                enabled=True,
                limits=ResearchLimitsConfig(
                    maximum_cases_per_run=3,
                    maximum_candidate_evidence=200,
                ),
                discovery=ResearchDiscoveryConfig(
                    minimum_evidence_count=2,
                    minimum_source_diversity=2,
                ),
            )
        )

    def test_all_four_versioned_benchmarks_are_point_in_time_safe(self):
        episodes = list_benchmarks()
        self.assertEqual(
            {episode.episode_id for episode in episodes},
            {
                "agricultural_supply_shock",
                "ai_infrastructure_expansion",
                "metaverse_marketing_noise",
                "monetary_policy_regime_change",
            },
        )
        self.assertEqual(
            {episode.episode_kind for episode in episodes},
            {"development", "macro_regime", "noise"},
        )
        for episode in episodes:
            self.assertEqual(episode.version, 1)
            self.assertTrue(episode.synthetic)
            for replay_as_of in episode.replay_dates:
                context = ResearchContext.replay(
                    replay_as_of, benchmark_id=episode.episode_id
                )
                visible = episode.evidence_as_of(context)
                self.assertTrue(visible)
                self.assertTrue(
                    all(item.available_at <= replay_as_of for item in visible)
                )
                context.guard_model_input(
                    {"evidence": [item.to_dict() for item in visible]},
                    stage="fixture_evidence",
                )
                with self.assertRaises(ReplayLeakageError):
                    context.guard_model_input(
                        episode.evaluator_payload(), stage="forbidden_answers"
                    )

    def test_four_controlled_episodes_run_through_full_replay_and_scorecard(self):
        outcomes = {}
        for episode in list_benchmarks():
            replay_as_of = episode.replay_dates[-1]
            context = ResearchContext.replay(
                replay_as_of,
                benchmark_id=episode.episode_id,
                run_id=f"controlled:{episode.episode_id}",
            )
            execution = execute_replay_research(
                episode.evidence,
                context,
                self.settings,
                stage_executor=ControlledStageExecutor(episode.episode_id),
            )
            scorecard = build_benchmark_scorecard(execution, episode)
            outcomes[episode.episode_id] = (execution, scorecard)
            self.assertEqual(
                scorecard["dimensions"]["point_in_time_integrity"]["status"],
                "pass",
            )
            self.assertEqual(execution.audit["leakage_violations"], [])
            self.assertGreaterEqual(execution.audit["future_evidence_excluded"], 1)
            self.assertIn(
                "claim_extraction",
                {item["stage"] for item in execution.stage_metrics},
            )

        noise, noise_score = outcomes["metaverse_marketing_noise"]
        self.assertEqual(noise.cases, ())
        self.assertGreater(noise.abstention_count, 0)
        self.assertEqual(noise.errors, ())
        self.assertEqual(noise_score["dimensions"]["specificity"]["status"], "pass")
        self.assertEqual(
            noise_score["dimensions"]["hypothesis_discovery"]["status"], "pass"
        )

        for episode_id in (
            "agricultural_supply_shock",
            "ai_infrastructure_expansion",
            "monetary_policy_regime_change",
        ):
            execution, scorecard = outcomes[episode_id]
            self.assertEqual(len(execution.cases), 1)
            case = execution.cases[0]
            self.assertTrue(case.case_is_economic_proposition)
            self.assertTrue(case.payload["causal_edges"])
            self.assertTrue(case.payload["value_capture"])
            self.assertIsNotNone(case.payload["adversarial"])
            self.assertIsNotNone(case.payload["deliverable"])
            self.assertTrue(case.payload["data_requests"])
            self.assertEqual(scorecard["dimensions"]["specificity"]["status"], "pass")
            self.assertEqual(
                scorecard["dimensions"]["causal_quality"]["status"], "pass"
            )
            self.assertIn(
                scorecard["dimensions"]["discovery"]["status"],
                {"pass", "partial"},
            )

    def test_hypothesis_discovery_factor_requires_relevant_testable_unknown(self):
        episode = next(
            item
            for item in list_benchmarks()
            if item.episode_id == "agricultural_supply_shock"
        )
        execution = execute_replay_research(
            episode.evidence,
            ResearchContext.replay(
                episode.replay_dates[-1], benchmark_id=episode.episode_id
            ),
            self.settings,
            stage_executor=ControlledStageExecutor(episode.episode_id),
        )
        scorecard = build_benchmark_scorecard(execution, episode)
        factor = scorecard["dimensions"]["hypothesis_discovery"]
        self.assertEqual(factor["status"], "pass")
        self.assertEqual(factor["measures"]["counter_hypothesis_count"], 1)
        self.assertEqual(factor["measures"]["data_request_count"], 1)
        self.assertTrue(factor["measures"]["expected_unknowns_matched"])
        self.assertEqual(
            scorecard["run_metrics"]["quality"]["graph"]["testable_hypothesis_rate"],
            1.0,
        )

        case = execution.cases[0]
        payload_without_request = {**case.payload, "data_requests": []}
        untestable = replace(
            execution,
            cases=(replace(case, payload=payload_without_request),),
        )
        untestable_factor = build_benchmark_scorecard(untestable, episode)[
            "dimensions"
        ]["hypothesis_discovery"]
        self.assertEqual(untestable_factor["status"], "partial")

    def test_configuration_overrides_are_bounded_and_do_not_mutate_base(self):
        base = AppConfig(
            database=DatabaseConfig(
                host="localhost",
                port=5432,
                name="test",
                user="research_runner_ci",
                password="s9V!q2K#x7Lm4P@t",
            ),
            llm=LlmConfig(api_key="rI8nW3qY5vT2mK7pL9sF4dH6"),
            research_intelligence=ResearchIntelligenceConfig(
                model_overrides={"pattern_discovery": "model/a"},
                stages={"pattern_discovery": ResearchStageConfig(enabled=True)},
            ),
        )
        changed = config_with_replay_overrides(
            base,
            model_overrides={"pattern_discovery": "model/b"},
            prompt_overrides={"pattern_discovery": "prompts/alternative.txt"},
        )
        self.assertEqual(
            base.research_intelligence.model_overrides["pattern_discovery"], "model/a"
        )
        self.assertEqual(
            changed.research_intelligence.model_overrides["pattern_discovery"],
            "model/b",
        )
        self.assertEqual(
            changed.research_intelligence.stages["pattern_discovery"].prompt_template,
            "prompts/alternative.txt",
        )
        with self.assertRaisesRegex(ValueError, "invalid research model override"):
            config_with_replay_overrides(base, model_overrides={"unknown": "model"})

    def test_variant_identity_uses_resolved_default_model(self):
        prompt_path = str(
            ORCH_ROOT.parent / "prompts" / "research_pattern_discovery_v2.txt"
        )
        base = AppConfig(
            database=DatabaseConfig(
                host="localhost",
                port=5432,
                name="test",
                user="research_runner_ci",
                password="s9V!q2K#x7Lm4P@t",
            ),
            llm=LlmConfig(
                api_key="rI8nW3qY5vT2mK7pL9sF4dH6",
                models={"default": "provider/model-a"},
            ),
            research_intelligence=ResearchIntelligenceConfig(
                enabled=True,
                stages={
                    "pattern_discovery": ResearchStageConfig(
                        prompt_template=prompt_path
                    )
                },
            ),
        )
        changed = AppConfig(
            database=DatabaseConfig(
                host="localhost",
                port=5432,
                name="test",
                user="research_runner_ci",
                password="s9V!q2K#x7Lm4P@t",
            ),
            llm=LlmConfig(
                api_key="rI8nW3qY5vT2mK7pL9sF4dH6",
                models={"default": "provider/model-b"},
            ),
            research_intelligence=ResearchIntelligenceConfig(
                enabled=True,
                stages={
                    "pattern_discovery": ResearchStageConfig(
                        prompt_template=prompt_path
                    )
                },
            ),
        )
        left = ResearchModelRunner(
            base, correlation_id=None, session=None
        ).cache_identity("pattern_discovery")
        right = ResearchModelRunner(
            changed, correlation_id=None, session=None
        ).cache_identity("pattern_discovery")
        self.assertEqual(left["model"], "provider/model-a")
        self.assertEqual(right["model"], "provider/model-b")
        self.assertNotEqual(
            canonical_fingerprint(left),
            canonical_fingerprint(right),
        )

    def test_model_stage_cache_is_keyed_by_prompt_and_model_identity(self):
        cache_identity = {
            "stage": "research_pattern_discovery_v2",
            "prompt": {"sha256": "prompt-a"},
            "model_override": "model/a",
        }
        runner = MagicMock()
        runner.cache_identity.return_value = cache_identity
        session = MagicMock()
        session.execute.return_value = Result(
            first={
                "attempt_id": "00000000-0000-4000-8000-000000000001",
                "raw_response": json.dumps({"ok": True}),
                "model_used": "model/a",
            }
        )
        result = run_model_stage(
            session,
            runner,
            "pattern_discovery",
            {"bounded": True},
            lambda value: value,
            "same-input",
        )
        self.assertTrue(result.provenance.metadata["reused"])
        runner.run.assert_not_called()
        statement, params = session.execute.call_args.args
        self.assertIn("request_metadata->'cache_identity'", str(statement))
        self.assertEqual(
            json.loads(params["cache_identity"]),
            cache_identity,
        )

        miss_session = MagicMock()
        miss_session.execute.return_value = Result(first=None)
        runner.run.return_value = ModelStageResult(
            value={"ok": True},
            provenance=ModelProvenance(model_slug="model/b"),
            cost_usd=0.01,
            tokens_input=1,
            tokens_output=1,
            duration_ms=1,
        )
        fresh = run_model_stage(
            miss_session,
            runner,
            "pattern_discovery",
            {"bounded": True},
            lambda value: value,
            "same-input",
        )
        self.assertEqual(fresh.cost_usd, 0.01)
        runner.run.assert_called_once()

    def test_human_annotations_are_validated_and_versioned(self):
        cleaned = validate_human_annotations(
            {
                "overall_label": "Partial",
                "dimension_labels": {"causal_quality": "PASS"},
                "notes": "Needs another independent source.",
            }
        )
        self.assertEqual(cleaned["overall_label"], "partial")
        self.assertEqual(cleaned["dimension_labels"], {"causal_quality": "pass"})
        with self.assertRaisesRegex(ValueError, "dimension is invalid"):
            validate_human_annotations(
                {"dimension_labels": {"unsupported_dimension": "pass"}}
            )
        with self.assertRaisesRegex(ValueError, "overall_label is invalid"):
            validate_human_annotations({"overall_label": "excellent"})

        session = MagicMock()
        session.execute.side_effect = [
            Result(first={"id": "scorecard-id", "annotation_version": 1}),
            Result(rowcount=1),
            Result(rowcount=1),
        ]
        replay_run_id = "11111111-1111-4111-8111-111111111111"
        result = annotate_benchmark_scorecard(
            session,
            replay_run_id,
            cleaned,
            annotated_by="reviewer@example.test",
            expected_version=1,
        )
        self.assertEqual(result["annotation_version"], 2)
        self.assertEqual(session.execute.call_count, 3)
        self.assertIn(
            "UPDATE research_benchmark_scorecards",
            str(session.execute.call_args_list[1].args[0]),
        )
        self.assertIn(
            "INSERT INTO research_benchmark_annotations",
            str(session.execute.call_args_list[2].args[0]),
        )

    def test_human_annotation_expected_version_prevents_stale_review(self):
        session = MagicMock()
        session.execute.return_value = Result(
            first={"id": "scorecard-id", "annotation_version": 2}
        )
        with self.assertRaisesRegex(ValueError, "version conflict"):
            annotate_benchmark_scorecard(
                session,
                "11111111-1111-4111-8111-111111111111",
                {"overall_label": "pass"},
                annotated_by="reviewer@example.test",
                expected_version=1,
            )
        self.assertEqual(session.execute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
