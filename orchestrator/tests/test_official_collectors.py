import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.cftc import CftcCollector
from collectors.base import CollectorNoData, CollectorSetupRequired
from collectors.official_macro import BoeCollector, EiaCollector, OecdCollector


class OfficialCollectorTests(unittest.TestCase):
    def test_configured_ecb_series_do_not_include_retired_5y5y_key(self):
        import yaml

        container_config = Path("/app/config/config.yaml")
        config_path = (
            container_config
            if container_config.exists()
            else Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        )
        config = yaml.safe_load(config_path.read_text())
        ids = {
            series["id"] for series in config["collectors"]["ecb"]["series"]
        }
        self.assertNotIn("INFLATION_5Y5Y", ids)

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
            ],
            "contracts": [{"market_id": "099741", "assets": ["EURUSD"]}],
        }}}
        records = CftcCollector().collect(config, "corr")
        self.assertEqual(records[0]["net_position"], 100)
        self.assertEqual(records[0]["net_pct_open_interest"], 10)
        self.assertEqual(records[0]["metadata"]["assets"], ["EURUSD"])
        self.assertIn("cftc_contract_market_code", request.call_args.kwargs["params"]["$where"])

    @patch("collectors.cftc.make_request")
    def test_cftc_ignores_unmapped_contracts(self, request):
        response = Mock()
        response.json.return_value = [{
            "cftc_contract_market_code": "OTHER",
            "report_date_as_yyyy_mm_dd": "2026-06-16",
        }]
        response.raise_for_status.return_value = None
        request.return_value = response
        config = {"collectors": {"cftc": {
            "url": "https://example.test",
            "categories": [],
            "contracts": [{"market_id": "099741", "assets": ["EURUSD"]}],
        }}}

        with self.assertRaises(CollectorNoData):
            CftcCollector().collect(config, "corr")

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
        self.assertIn("acquired_at", records[0])

    def test_oecd_parses_current_sdmx_csv_shape(self):
        response = Mock()
        response.text = (
            "DATAFLOW,REF_AREA,FREQ,MEASURE,UNIT_MEASURE,ACTIVITY,ADJUSTMENT,"
            "TRANSFORMATION,TIME_HORIZ,METHODOLOGY,TIME_PERIOD,OBS_VALUE\n"
            "OECD.SDD.STES:DSD_STES@DF_CLI(4.1),USA,M,LI,IX,_Z,AA,IX,_Z,H,"
            "2026-04,100.12\n"
        )
        series = {
            "id": "CLI_US",
            "format": "csv",
            "date_field": "TIME_PERIOD",
            "value_field": "OBS_VALUE",
            "frequency": "monthly",
            "region": "US",
        }

        records = OecdCollector()._parse(response, series)

        self.assertEqual(records[0]["series_id"], "OECD:CLI_US")
        self.assertEqual(records[0]["value"], 100.12)

    def test_boe_parses_current_iadb_csv_shape(self):
        response = Mock()
        response.text = "DATE,IUDBEDR\n02 Jan 2026,3.75\n"
        series = {
            "id": "BANK_RATE",
            "provider_series": "IUDBEDR",
            "format": "csv",
            "date_field": "DATE",
            "date_format": "%d %b %Y",
            "value_field": "IUDBEDR",
            "frequency": "daily",
            "region": "GB",
        }

        records = BoeCollector()._parse(response, series)

        self.assertEqual(records[0]["series_id"], "BOE:BANK_RATE")
        self.assertEqual(records[0]["value"], 3.75)

    def test_official_collector_requires_configured_series(self):
        with self.assertRaises(CollectorSetupRequired):
            OecdCollector().collect(
                {"collectors": {"oecd": {"series": []}}},
                "corr",
            )

    def test_eia_requires_explicit_api_key(self):
        config = {"collectors": {"eia": {
            "requires_api_key": True,
            "api_key": "",
            "series": [{"id": "BRENT", "url": "https://example.test"}],
        }}}
        with self.assertRaises(CollectorSetupRequired):
            EiaCollector().collect(config, "corr")

    @patch("collectors.official_macro.make_request")
    def test_official_collector_reports_partial_series_failure(self, request):
        good = Mock()
        good.raise_for_status.return_value = None
        good.json.return_value = {"rows": [{"date": "2026-05", "value": "101.2"}]}
        request.side_effect = [good, RuntimeError("rate limited")]
        config = {"collectors": {"oecd": {"series": [
            {
                "id": "CLI_US", "url": "https://example.test/us",
                "records_path": ["rows"], "date_field": "date",
                "value_field": "value",
            },
            {
                "id": "CLI_GB", "url": "https://example.test/gb",
                "records_path": ["rows"], "date_field": "date",
                "value_field": "value",
            },
        ]}}}
        collector = OecdCollector()

        records = collector.collect(config, "corr")

        self.assertEqual(len(records), 1)
        self.assertEqual(collector.last_result_metadata["state"], "partial")
        self.assertEqual(
            collector.last_result_metadata["series_failed"][0]["series_id"],
            "CLI_GB",
        )


if __name__ == "__main__":
    unittest.main()
