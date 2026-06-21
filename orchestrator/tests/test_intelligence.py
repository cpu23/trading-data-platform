import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from processors._validators import OutputPolicyError, scan_prohibited_language
from processors.intelligence import MarketIntelligenceProcessor


EVIDENCE_ID = "opinion:11111111-1111-1111-1111-111111111111"
SYMBOLS = ["EURUSD"]


def claim(claim_id, text_value="Restrictive policy supports the dollar."):
    return {
        "claim_id": claim_id,
        "text": text_value,
        "evidence_ids": [EVIDENCE_ID],
    }


def valid_role(role):
    return {
        "global": {
            "bias": "mixed",
            "confidence": "moderate",
            "claims": [claim(f"{role}.global.1")],
            "contradictions": [],
        },
        "assets": [
            {
                "symbol": "EURUSD",
                "bias": "bearish",
                "confidence": "moderate",
                "claims": [claim(f"{role}.asset.EURUSD.1")],
                "contradictions": [],
            }
        ],
    }


def narrative(source_claim_id, text_value="Relative policy expectations favor the dollar."):
    return {
        "text": text_value,
        "source_claim_ids": [source_claim_id],
        "evidence_ids": [EVIDENCE_ID],
    }


def valid_editor():
    return {
        "global": {
            "bias": "mixed",
            "confidence": "moderate",
            "summary": narrative("analyst.global.1", "Growth and policy signals are mixed."),
            "drivers": [narrative("analyst.global.1")],
            "contradictions": [],
            "invalidation_conditions": [],
        },
        "assets": [
            {
                "symbol": "EURUSD",
                "bias": "bearish",
                "confidence": "moderate",
                "summary": narrative("analyst.asset.EURUSD.1"),
                "drivers": [narrative("analyst.asset.EURUSD.1")],
                "contradictions": [],
                "invalidation_conditions": [],
                "disagreements": [],
            }
        ],
    }


def llm_result(value, tokens_input=1, tokens_output=2, cost=0.001):
    return {
        "content": json.dumps(value),
        "model": "test-model",
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "cost_usd": cost,
        "duration_ms": 10,
        "request_metadata": {},
    }


class IntelligenceSchemaTests(unittest.TestCase):
    def setUp(self):
        self.processor = MarketIntelligenceProcessor()

    def test_role_schema_rejects_extra_keys_bad_enums_and_unknown_evidence(self):
        value = valid_role("analyst")
        value["global"]["extra"] = True
        value["global"]["bias"] = "strongly bullish"
        value["assets"][0]["claims"][0]["evidence_ids"] = ["invented:1"]
        issues = self.processor._validate_role(
            value, SYMBOLS, "analyst", {EVIDENCE_ID}
        )
        self.assertTrue(any("unexpected keys" in issue for issue in issues))
        self.assertTrue(any("invalid value" in issue for issue in issues))
        self.assertTrue(any("unsupported id" in issue for issue in issues))

    def test_role_allows_asset_without_direct_evidence_claims(self):
        value = valid_role("analyst")
        value["assets"][0]["claims"] = []

        issues = self.processor._validate_role(
            value, SYMBOLS, "analyst", {EVIDENCE_ID}
        )

        self.assertEqual(issues, [])

    def test_role_claim_ids_are_canonicalized_before_validation(self):
        value = valid_role("analyst")
        value["assets"][0]["claims"][0]["claim_id"] = "EURUSD-thesis"

        issues = self.processor._validate_prepared_role(
            value, SYMBOLS, "analyst", {EVIDENCE_ID}
        )

        self.assertEqual(issues, [])
        self.assertEqual(
            value["assets"][0]["claims"][0]["claim_id"],
            "analyst.asset.EURUSD.1",
        )

    def test_role_drops_positioning_not_mapped_to_asset(self):
        value = valid_role("analyst")
        value["assets"][0]["claims"][0]["evidence_ids"] = [
            "positioning:cftc:other:2026-06-09:dealer"
        ]

        issues = self.processor._validate_prepared_role(
            value,
            SYMBOLS,
            "analyst",
            {EVIDENCE_ID, "positioning:cftc:other:2026-06-09:dealer"},
            {"EURUSD": [EVIDENCE_ID]},
        )

        self.assertEqual(issues, [])
        self.assertEqual(value["assets"][0]["claims"], [])

    def test_role_normalizes_description_claim_shape(self):
        value = valid_role("analyst")
        value["assets"][0]["contradictions"] = [
            {
                "contradiction_id": "x",
                "description": "Evidence points in opposite directions.",
                "evidence_ids": [EVIDENCE_ID],
            }
        ]

        issues = self.processor._validate_prepared_role(
            value, SYMBOLS, "analyst", {EVIDENCE_ID}
        )

        self.assertEqual(issues, [])
        self.assertEqual(
            value["assets"][0]["contradictions"][0]["text"],
            "Evidence points in opposite directions.",
        )

    def test_role_normalizes_claim_reference_contradiction(self):
        value = valid_role("analyst")
        claim_id = value["assets"][0]["claims"][0]["claim_id"]
        value["assets"][0]["contradictions"] = [
            {
                "contradiction_id": "x",
                "claim_ids": [claim_id],
            }
        ]

        issues = self.processor._validate_prepared_role(
            value, SYMBOLS, "analyst", {EVIDENCE_ID}
        )

        self.assertEqual(issues, [])
        contradiction = value["assets"][0]["contradictions"][0]
        self.assertIn("conflicting economic pressures", contradiction["text"])
        self.assertEqual(contradiction["evidence_ids"], [EVIDENCE_ID])

    def test_editor_rejects_claims_not_supported_by_roles(self):
        roles = {role: valid_role(role) for role in ("analyst", "skeptic", "auditor")}
        value = valid_editor()
        value["global"]["drivers"][0]["source_claim_ids"] = ["invented.claim"]
        issues = self.processor._validate_editor(value, SYMBOLS, roles)
        self.assertTrue(any("unsupported id 'invented.claim'" in issue for issue in issues))

    def test_editor_rejects_evidence_not_shared_by_cited_claims(self):
        roles = {role: valid_role(role) for role in ("analyst", "skeptic", "auditor")}
        value = valid_editor()
        value["global"]["drivers"][0]["evidence_ids"] = ["event:invented"]
        issues = self.processor._validate_editor(value, SYMBOLS, roles)
        self.assertTrue(any("unsupported id 'event:invented'" in issue for issue in issues))

    def test_editor_accepts_union_of_evidence_from_cited_claims(self):
        roles = {role: valid_role(role) for role in ("analyst", "skeptic", "auditor")}
        second_evidence = "positioning:cftc:097741:2026-06-09:dealer"
        roles["skeptic"]["global"]["claims"][0]["evidence_ids"] = [second_evidence]
        value = valid_editor()
        value["global"]["summary"]["source_claim_ids"] = [
            "analyst.global.1",
            "skeptic.global.1",
        ]
        value["global"]["summary"]["evidence_ids"] = [EVIDENCE_ID, second_evidence]

        issues = self.processor._validate_editor(value, SYMBOLS, roles)

        self.assertEqual(issues, [])

    def test_editor_summary_is_evidence_bounded_and_normalized(self):
        roles = {role: valid_role(role) for role in ("analyst", "skeptic", "auditor")}
        value = valid_editor()
        self.assertEqual(self.processor._validate_editor(value, SYMBOLS, roles), [])
        normalized = self.processor._normalize_editor(value)
        self.assertEqual(normalized["global"]["summary"], "Growth and policy signals are mixed.")
        self.assertEqual(
            normalized["global"]["summary_evidence"]["evidence_ids"],
            [EVIDENCE_ID],
        )
        self.assertEqual(
            normalized["global"]["drivers"],
            ["Relative policy expectations favor the dollar."],
        )
        self.assertEqual(
            normalized["global"]["drivers_evidence"][0]["evidence_ids"],
            [EVIDENCE_ID],
        )

    def test_editor_repairs_empty_evidence_and_drops_unreferenced_optional_items(self):
        roles = {role: valid_role(role) for role in ("analyst", "skeptic", "auditor")}
        value = valid_editor()
        value["global"]["summary"]["evidence_ids"] = []
        value["assets"][0]["contradictions"] = [
            {
                "text": "Unsupported optional detail.",
                "source_claim_ids": [],
                "evidence_ids": [],
            }
        ]

        issues = self.processor._validate_prepared_editor(value, SYMBOLS, roles)

        self.assertEqual(issues, [])
        self.assertEqual(value["global"]["summary"]["evidence_ids"], [EVIDENCE_ID])
        self.assertEqual(value["assets"][0]["contradictions"], [])

    def test_editor_does_not_repair_unsupported_nonempty_references(self):
        roles = {role: valid_role(role) for role in ("analyst", "skeptic", "auditor")}
        value = valid_editor()
        value["global"]["summary"]["evidence_ids"] = ["event:invented"]

        issues = self.processor._validate_prepared_editor(value, SYMBOLS, roles)

        self.assertEqual(issues, [])
        self.assertEqual(value["global"]["summary"]["evidence_ids"], [EVIDENCE_ID])

    def test_editor_fills_sparse_asset_summary_from_global_claim(self):
        roles = {role: valid_role(role) for role in ("analyst", "skeptic", "auditor")}
        for role_value in roles.values():
            role_value["assets"][0]["claims"] = []
        value = valid_editor()
        value["assets"][0]["summary"] = {
            "text": "",
            "source_claim_ids": [],
            "evidence_ids": [],
        }

        issues = self.processor._validate_prepared_editor(value, SYMBOLS, roles)

        self.assertEqual(issues, [])
        self.assertEqual(value["assets"][0]["bias"], "neutral")
        self.assertEqual(value["assets"][0]["confidence"], "low")
        self.assertIn("Insufficient direct evidence", value["assets"][0]["summary"]["text"])

    def test_prompts_delimit_untrusted_data_and_align_policy_vocabulary(self):
        context = {
            "symbols": SYMBOLS,
            "evidence": [{"evidence_id": EVIDENCE_ID, "value": {}}],
        }
        prompt = self.processor._role_prompt(
            "analyst", "Assess economic evidence.", context
        ).lower()
        self.assertIn("<untrusted_evidence>", prompt)
        self.assertIn("price action", prompt)
        self.assertIn("technical analysis", prompt)
        self.assertIn("position sizing", prompt)
        self.assertIn("portfolio allocation", prompt)
        self.assertIn("at most 2 claims", prompt)
        self.assertIn("risk appetite with physical industrial demand", prompt)
        self.assertIn("weaker dollar as a bearish force", prompt)
        self.assertIn("exactly one participant category", prompt)

        editor_prompt = self.processor._editor_prompt(
            context,
            {role: valid_role(role) for role in ("analyst", "skeptic", "auditor")},
        ).lower()
        self.assertIn("risk appetite is not physical industrial demand", editor_prompt)
        self.assertIn("weaker dollar cannot be presented as bearish", editor_prompt)

    def test_editor_drops_optional_narrative_when_any_source_claim_is_unknown(self):
        roles = {role: valid_role(role) for role in ("analyst", "skeptic", "auditor")}
        value = valid_editor()
        value["assets"][0]["disagreements"] = [{
            "text": "This sentence still describes an invented analyst claim.",
            "source_claim_ids": [
                "analyst.asset.EURUSD.999",
                "auditor.asset.EURUSD.1",
            ],
            "evidence_ids": [EVIDENCE_ID],
        }]

        issues = self.processor._validate_prepared_editor(value, SYMBOLS, roles)

        self.assertEqual(issues, [])
        self.assertEqual(value["assets"][0]["disagreements"], [])

    def test_editor_drops_narrative_that_cites_a_claim_from_another_scope(self):
        roles = {role: valid_role(role) for role in ("analyst", "skeptic", "auditor")}
        value = valid_editor()
        value["global"]["drivers"] = [{
            "text": "Asset-specific positioning must not become a global driver.",
            "source_claim_ids": ["analyst.asset.EURUSD.1"],
            "evidence_ids": [EVIDENCE_ID],
        }]

        issues = self.processor._validate_prepared_editor(value, SYMBOLS, roles)

        self.assertEqual(issues, [])
        self.assertEqual(value["global"]["drivers"], [])

    def test_stage_profile_supports_role_specific_model_and_provider(self):
        config = {
            "llm": {
                "intelligence_roles": {
                    "skeptic": {
                        "model": "openai/gpt-oss-120b",
                        "reasoning_effort": "medium",
                        "max_tokens": 2400,
                        "provider": {
                            "order": ["WandB"],
                            "allow_fallbacks": False,
                        },
                    }
                }
            }
        }

        profile = self.processor._stage_profile(config, "skeptic", "fallback")

        self.assertEqual(profile["model"], "openai/gpt-oss-120b")
        self.assertEqual(profile["call_options"]["max_tokens"], 2400)
        self.assertEqual(
            profile["call_options"]["provider_preferences"]["order"], ["WandB"]
        )


class IntelligenceRepairTests(unittest.TestCase):
    @patch.object(MarketIntelligenceProcessor, "_record_attempt")
    @patch("processors.intelligence.call_llm")
    def test_repairs_once_and_accounts_for_both_attempts(
        self, call_llm, record_attempt
    ):
        processor = MarketIntelligenceProcessor()
        invalid = valid_role("analyst")
        invalid["global"]["claims"][0]["text"] = "Price action confirms a breakout."
        repaired = valid_role("analyst")
        call_llm.side_effect = [llm_result(invalid), llm_result(repaired, 3, 4, 0.002)]

        parsed, attempts = processor._generate_validated(
            "analyst",
            "prompt",
            lambda value: processor._validate_role(
                value, SYMBOLS, "analyst", {EVIDENCE_ID}
            ),
            "test-model",
            {},
            "11111111-1111-1111-1111-111111111111",
        )

        self.assertEqual(parsed, repaired)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(call_llm.call_count, 2)
        self.assertEqual(record_attempt.call_count, 2)

    @patch.object(MarketIntelligenceProcessor, "_record_attempt")
    @patch("processors.intelligence.call_llm")
    def test_failed_repair_raises_validation_failed_not_quarantine(
        self, call_llm, record_attempt
    ):
        invalid = valid_role("analyst")
        invalid["global"]["claims"][0]["text"] = "Use price action to buy EURUSD."
        call_llm.return_value = llm_result(invalid)
        processor = MarketIntelligenceProcessor()
        with self.assertRaises(OutputPolicyError) as raised:
            processor._generate_validated(
                "analyst",
                "prompt",
                lambda value: processor._validate_role(
                    value, SYMBOLS, "analyst", {EVIDENCE_ID}
                ),
                "test-model",
                {},
                "11111111-1111-1111-1111-111111111111",
            )
        self.assertIn("validation_failed", str(raised.exception))
        self.assertNotIn("quarantined", str(raised.exception))
        self.assertEqual(call_llm.call_count, 2)
        self.assertEqual(record_attempt.call_count, 2)

    def test_repair_prompt_contains_invalid_response(self):
        prompt = MarketIntelligenceProcessor._repair_prompt(
            "original schema",
            '{"contradictions":[{"claim_id":"x"}]}',
            ["missing keys: text"],
        )
        self.assertIn("<INVALID_JSON>", prompt)
        self.assertIn('"claim_id":"x"', prompt)
        self.assertIn("missing keys: text", prompt)


class IntelligenceDeltaTests(unittest.TestCase):
    def setUp(self):
        self.processor = MarketIntelligenceProcessor()

    def test_delta_covers_global_and_asset_narrative_fields(self):
        previous = {
            "payload": {
                "bias": "neutral",
                "confidence": "low",
                "summary": "Old summary.",
                "drivers": [narrative("analyst.global.1", "Old driver.")],
                "contradictions": [],
                "invalidation_conditions": [],
                "assets": [
                    {
                        "symbol": "EURUSD",
                        "bias": "neutral",
                        "confidence": "low",
                        "summary": "Old asset summary.",
                        "drivers": [],
                        "contradictions": [],
                        "invalidation_conditions": [],
                    }
                ],
            }
        }
        edited = self.processor._normalize_editor(valid_editor())
        delta = self.processor._delta(previous, edited)
        self.assertTrue(delta["material_change"])
        self.assertIn("bias", delta["global_delta"])
        self.assertIn("confidence", delta["global_delta"])
        self.assertIn("drivers", delta["global_delta"])
        self.assertEqual(delta["asset_deltas"][0]["symbol"], "EURUSD")

    def test_exact_same_assessment_is_no_material_change(self):
        edited = self.processor._normalize_editor(valid_editor())
        previous = {"payload": json.loads(json.dumps(edited))}
        delta = self.processor._delta(previous, edited)
        self.assertFalse(delta["material_change"])
        self.assertEqual(delta["changed"], [])

    def test_no_change_uses_no_paid_calls_and_keeps_baseline(self):
        context = {
            "symbols": SYMBOLS,
            "evidence": [{"evidence_id": EVIDENCE_ID, "value": {}}],
            "evidence_ids": [EVIDENCE_ID],
            "opinion_ids": [EVIDENCE_ID.split(":", 1)[1]],
            "event_ids": [],
            "positioning_ids": [],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {"symbols": SYMBOLS, "evidence": context["evidence"]},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        previous = {
            "opinion_id": "22222222-2222-2222-2222-222222222222",
            "payload": {
                "input_fingerprint": fingerprint,
                "bias": "mixed",
                "confidence": "moderate",
                "assets": [],
            },
        }
        with patch.object(self.processor, "_context", return_value=context), patch.object(
            self.processor, "_previous", return_value=previous
        ), patch("processors.intelligence.call_llm") as llm:
            result = self.processor.process(
                {"llm": {}, "watchlist": {"trading": []}}, "corr"
            )
        llm.assert_not_called()
        self.assertEqual(result["processing_log"]["cost_usd"], 0.0)
        self.assertTrue(
            result["processing_log"]["request_metadata"]["paid_inference_skipped"]
        )
        self.assertEqual(
            result["opinions"][0]["baseline_opinion_id"],
            previous["opinion_id"],
        )


class IntelligencePolicyTests(unittest.TestCase):
    def test_scanner_and_prompt_share_price_action_boundary(self):
        findings = scan_prohibited_language(
            {"summary": "Price action suggests a bullish economic bias."}
        )
        self.assertTrue(any("technical_analysis" in item for item in findings))

    def test_scanner_rejects_direct_and_indirect_advice_language(self):
        examples = [
            "Buy EURUSD now.",
            "You should sell gold.",
            "Consider going long after the release.",
            "Increase your exposure to equities.",
            "Overweight bonds.",
        ]
        for example in examples:
            with self.subTest(example=example):
                self.assertTrue(scan_prohibited_language(example))

    def test_scanner_allows_economic_exposure_and_allocation_context(self):
        examples = [
            "Banks reduced credit exposure during the slowdown.",
            "Corporate capital allocation favored productive investment.",
            "Exporters sell dollars received through normal operations.",
        ]
        for example in examples:
            with self.subTest(example=example):
                self.assertEqual(scan_prohibited_language(example), [])

    def test_generation_attempt_migration_is_durable_and_retained_90_days(self):
        migration = (
            Path(__file__).resolve().parents[2]
            / "db"
            / "migrations"
            / "011_generation_attempts.sql"
        ).read_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS generation_attempts", migration)
        self.assertIn("'validation_failed'", migration)
        self.assertIn("retention_days INTEGER DEFAULT 90", migration)


if __name__ == "__main__":
    unittest.main()
