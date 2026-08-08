import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processors.base import canonical_fingerprint
from processors.briefing import DailyBriefingProcessor

CONFIG = {
    "timezone": {
        "primary": {"name": "Europe/London", "label": "London"},
        "secondary": {"name": "America/New_York", "label": "NY"},
    },
    "watchlist": {"trading": []},
    "processors": {"briefing": {"prompt_template": "prompts/briefing_v5.txt"}},
}

TEMPLATE_WITH_ATOMS = (
    "Date: {{current_date}}\n{{macro_regime_summary}}\n{{today_events}}\n"
    "{{this_week_events}}\n{{watchlist}}\n{{asset_context}}\n"
    "{{investment_news}}\n{{previous_briefing}}\n{{current_atoms}}"
)

VALID_RESPONSE = json.dumps(
    {
        "what_changed": "Nothing material changed.",
        "interpretation": "Rates stay restrictive.",
        "invalidation": "A soft CPI print.",
        "watchlist_notes": [],
    }
)

ATOM_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _atom_row(claim, fingerprint, atom_id=ATOM_ID, evidence=None):
    """One current-atom row shaped like atoms.current_atoms output."""
    return {
        "id": atom_id,
        "subject_type": "macro",
        "subject_id": "US",
        "claim_type": "regime",
        "claim": claim,
        "observation_text": f"Observed: {claim}",
        "interpretation_text": f"Interpretation of {claim}",
        "scenario_text": None,
        "unknowns": [],
        "affected_assets": [],
        "time_horizon": "2w",
        "confidence": 0.8,
        "confidence_components": {},
        "valid_from": "2026-08-01T00:00:00+00:00",
        "expires_at": None,
        "carry_forward": False,
        "invalidation_conditions": [],
        "status": "published",
        "supersedes_atom_id": None,
        "source_event_id": None,
        "prompt_version": "v1",
        "model_slug": "test-model",
        "generation_attempt_id": None,
        "input_fingerprint": fingerprint,
        "created_at": "2026-08-01T00:00:00+00:00",
        "published_at": "2026-08-01T00:00:00+00:00",
        "evidence": evidence
        if evidence is not None
        else [
            {
                "evidence_type": "macro_series",
                "evidence_id": "FEDFUNDS",
                "relationship": "supports",
                "excerpt": "5.33%",
                "source_timestamp": "2026-08-01T00:00:00+00:00",
            }
        ],
    }


def _mock_session_with_atoms(atoms):
    session = Mock()
    result = Mock()
    result.fetchone.return_value = None
    result.mappings.return_value.all.return_value = atoms
    session.execute.return_value = result
    return session


class BriefingAtomsTests(unittest.TestCase):
    def test_fingerprint_inputs_include_atom_marker_and_change_with_atom_set(self):
        processor = DailyBriefingProcessor()

        def inputs_for(atoms):
            session = _mock_session_with_atoms(atoms)
            with patch("processors.briefing.get_session") as get_session:
                get_session.return_value.__enter__.return_value = session
                return processor.get_fingerprint_inputs(CONFIG)

        first = _atom_row("Fed on hold", "a" * 64)
        changed = _atom_row(
            "Fed on hold", "b" * 64, atom_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        )

        inputs_a = inputs_for([first])
        inputs_a_again = inputs_for([first])
        inputs_b = inputs_for([changed])

        self.assertIn("atoms", inputs_a)
        self.assertEqual(inputs_a["atoms"]["count"], 1)
        self.assertRegex(inputs_a["atoms"]["fingerprint"], r"^[0-9a-f]{64}$")

        # Identical atom sets reuse the same marker; changed sets do not.
        self.assertEqual(
            inputs_a["atoms"]["fingerprint"], inputs_a_again["atoms"]["fingerprint"]
        )
        self.assertNotEqual(
            inputs_a["atoms"]["fingerprint"], inputs_b["atoms"]["fingerprint"]
        )
        # The full processor input marker changes so the report is not reused.
        self.assertNotEqual(
            canonical_fingerprint(inputs_a), canonical_fingerprint(inputs_b)
        )

    @patch("processors.briefing.assemble_atom_context")
    @patch("processors.briefing.get_session")
    def test_atom_limit_read_from_processor_config(self, get_session, assemble):
        processor = DailyBriefingProcessor()
        assemble.return_value = []
        get_session.return_value.__enter__.return_value = _mock_session_with_atoms([])

        processor.get_fingerprint_inputs(
            {
                **CONFIG,
                "processors": {"briefing": {"max_atoms": 7}},
            }
        )
        self.assertEqual(assemble.call_args.kwargs["limit"], 7)

        processor.get_fingerprint_inputs(CONFIG)
        self.assertEqual(assemble.call_args.kwargs["limit"], 30)

    @patch("processors.briefing.load_prompt_template")
    def test_prompt_builder_includes_atom_claims_when_provided(
        self, load_prompt_template
    ):
        load_prompt_template.return_value = (TEMPLATE_WITH_ATOMS, {})
        processor = DailyBriefingProcessor()

        atom_section = processor._format_atom_section(
            [_atom_row("Fed on hold", "a" * 64)]
        )
        parsed = json.loads(atom_section)
        self.assertEqual(parsed[0]["evidence_ids"], ["FEDFUNDS"])
        self.assertEqual(parsed[0]["subject_id"], "US")
        self.assertEqual(parsed[0]["confidence"], 0.8)

        prompt = processor._build_prompt(
            template_path="any",
            current_date="Thursday, August 06, 2026",
            macro_regime_summary="Steady.",
            today_events="none",
            this_week_events="none",
            watchlist="EURUSD (forex)",
            current_atoms=atom_section,
        )

        self.assertIn("Fed on hold", prompt)
        self.assertIn("Interpretation of Fed on hold", prompt)
        self.assertNotIn("{{current_atoms}}", prompt)

    @patch("processors.briefing.load_prompt_template")
    def test_prompt_builder_omits_atom_section_when_empty(self, load_prompt_template):
        load_prompt_template.return_value = (TEMPLATE_WITH_ATOMS, {})
        processor = DailyBriefingProcessor()

        prompt = processor._build_prompt(
            template_path="any",
            current_date="Thursday, August 06, 2026",
            macro_regime_summary="Steady.",
            today_events="none",
            this_week_events="none",
            watchlist="EURUSD (forex)",
        )

        self.assertNotIn("{{current_atoms}}", prompt)
        self.assertNotIn("Fed on hold", prompt)
        self.assertNotIn("evidence_ids", prompt)

    @patch("processors.briefing.call_llm")
    @patch("processors.briefing.load_classified_news")
    @patch("processors.briefing.load_prompt_template")
    @patch("processors.briefing.assemble_atom_context")
    @patch("processors.briefing.get_session")
    @patch("processors.briefing.LLMStage")
    def test_atom_query_failure_leaves_briefing_unchanged(
        self,
        llm_stage,
        get_session,
        assemble_atom_context,
        load_prompt_template,
        load_classified_news,
        call_llm,
    ):
        assemble_atom_context.side_effect = RuntimeError("db unavailable")
        load_prompt_template.return_value = (TEMPLATE_WITH_ATOMS, {})
        load_classified_news.return_value = []
        get_session.return_value.__enter__.return_value = _mock_session_with_atoms([])
        stage = llm_stage.return_value
        stage.policy.model = "test-model"
        stage.call.return_value = {
            "content": VALID_RESPONSE,
            "model": "test-model",
            "tokens_input": 10,
            "tokens_output": 5,
            "cost_usd": 0.0,
        }
        stage.telemetry.tokens_input_total = 10
        stage.telemetry.tokens_output_total = 5
        stage.telemetry.cost_usd_total = 0.0
        stage.telemetry.as_dict.return_value = {}

        result = DailyBriefingProcessor().process(CONFIG, "test-corr")

        self.assertEqual(result["processing_log"]["status"], "success")
        self.assertIn("opinion_id", result["opinion"])
        stage.call.assert_called_once()
        call_llm.assert_not_called()

        prompt = stage.call.call_args.args[0]
        self.assertNotIn("Fed on hold", prompt)
        self.assertNotIn("{{current_atoms}}", prompt)
        assemble_atom_context.assert_called_once()
        self.assertEqual(assemble_atom_context.call_args.kwargs["limit"], 30)

    @patch("processors.briefing.call_llm")
    @patch("processors.briefing.load_classified_news")
    @patch("processors.briefing.load_prompt_template")
    @patch("processors.briefing.assemble_atom_context")
    @patch("processors.briefing.get_session")
    @patch("processors.briefing.LLMStage")
    def test_atoms_feed_the_single_existing_model_call(
        self,
        llm_stage,
        get_session,
        assemble_atom_context,
        load_prompt_template,
        load_classified_news,
        call_llm,
    ):
        assemble_atom_context.return_value = [_atom_row("Fed on hold", "a" * 64)]
        load_prompt_template.return_value = (TEMPLATE_WITH_ATOMS, {})
        load_classified_news.return_value = []
        get_session.return_value.__enter__.return_value = _mock_session_with_atoms([])
        stage = llm_stage.return_value
        stage.policy.model = "test-model"
        stage.call.return_value = {
            "content": VALID_RESPONSE,
            "model": "test-model",
            "tokens_input": 10,
            "tokens_output": 5,
            "cost_usd": 0.0,
        }
        stage.telemetry.tokens_input_total = 10
        stage.telemetry.tokens_output_total = 5
        stage.telemetry.cost_usd_total = 0.0
        stage.telemetry.as_dict.return_value = {}

        result = DailyBriefingProcessor().process(CONFIG, "test-corr")

        self.assertEqual(result["processing_log"]["status"], "success")
        # Exactly one narrative-synthesis call; no additional model path.
        stage.call.assert_called_once()
        call_llm.assert_not_called()

        prompt = stage.call.call_args.args[0]
        self.assertIn("Fed on hold", prompt)
        self.assertNotIn("{{current_atoms}}", prompt)


if __name__ == "__main__":
    unittest.main()
