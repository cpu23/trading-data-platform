import unittest
from unittest.mock import patch

from processors.intelligence import MarketIntelligenceProcessor


class IntelligenceTests(unittest.TestCase):
    def test_delta_retains_disagreement_as_change(self):
        processor = MarketIntelligenceProcessor()
        previous = {"payload": {"assets": [{"symbol": "EURUSD", "bias": "bearish"}]}}
        edited = {"global": {"drivers": []}, "assets": [{"symbol": "EURUSD", "bias": "bullish"}]}
        delta = processor._delta(previous, edited)
        self.assertTrue(delta["material_change"])
        self.assertEqual(delta["changed"][0]["from"], "bearish")

    def test_no_change_uses_no_paid_calls(self):
        processor = MarketIntelligenceProcessor()
        context = {"symbols": ["EURUSD"], "regime": {}, "events": [], "positioning": []}
        import hashlib, json
        fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        previous = {"payload": {"input_fingerprint": fingerprint, "assets": []}}
        with patch.object(processor, "_context", return_value=context), \
             patch.object(processor, "_previous", return_value=previous), \
             patch("processors.intelligence.call_llm") as llm:
            result = processor.process({"llm": {}, "watchlist": {"trading": []}}, "corr")
        llm.assert_not_called()
        self.assertEqual(result["processing_log"]["cost_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()
