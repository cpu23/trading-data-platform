import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.cftc import CftcCollector
from collectors.official_macro import OecdCollector


class OfficialCollectorTests(unittest.TestCase):
    @patch("collectors.cftc.make_request")
    def test_cftc_normalizes_positioning(self, request):
        response = Mock()
        response.json.return_value = [{
            "cftc_contract_market_code": "099741",
            "contract_market_name": "EURO FX",
            "report_date_as_yyyy_mm_dd": "2026-06-16",
            "open_interest_all": "1000",
            "dealer_positions_long_all": "300",
            "dealer_positions_short_all": "200",
        }]
        response.raise_for_status.return_value = None
        request.return_value = response
        config = {"collectors": {"cftc": {
            "url": "https://example.test", "categories": [
                ["dealer", "dealer_positions_long_all", "dealer_positions_short_all"]
            ]
        }}}
        records = CftcCollector().collect(config, "corr")
        self.assertEqual(records[0]["net_position"], 100)
        self.assertEqual(records[0]["net_pct_open_interest"], 10)

    def test_official_macro_namespaces_series_and_preserves_semantics(self):
        response = Mock()
        response.json.return_value = {"rows": [{"date": "2026-05-01", "value": "101.2"}]}
        series = {
            "id": "CLI_US", "format": "json", "records_path": ["rows"],
            "date_field": "date", "value_field": "value",
            "semantic_feature": "growth.us", "region": "US",
        }
        records = OecdCollector()._parse(response, series)
        self.assertEqual(records[0]["series_id"], "OECD:CLI_US")
        self.assertEqual(records[0]["metadata"]["semantic_feature"], "growth.us")


if __name__ == "__main__":
    unittest.main()
