import base64
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DASHBOARD_USER", "internal-user")
os.environ.setdefault("DASHBOARD_PASSWORD", "internal-pass")
os.environ.setdefault("DEPLOYMENT_MODE", "test")
INTERNAL_AUTH = {
    "Authorization": "Basic "
    + base64.b64encode(b"internal-user:internal-pass").decode()
}

from config_loader import (  # noqa: E402
    ConfigError,
    config_version,
    load_config,
    reload_config,
    restart_required,
    restart_sensitive_changes,
)
from contracts.runtime_config import ConfigStore, committed_config_paths  # noqa: E402
from data_quality import evaluate_quality, required_quality_checks  # noqa: E402


def _write_config(path: Path, extra: str = "") -> None:
    path.write_text(
        "database:\n"
        "  host: localhost\n"
        "  port: 5432\n"
        "  name: test\n"
        "  user: runtime-user\n"
        "  password: correct-horse-battery\n"
        f"{extra}"
    )


class ConfigLoaderValidationTests(unittest.TestCase):
    def test_unknown_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path, extra="mystery_section: 1\n")
            with self.assertRaises(ConfigError):
                load_config(str(path))

    def test_semantic_validation_reports_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path, extra="orchestration:\n  collector_workers: 0\n")
            with self.assertRaisesRegex(ConfigError, "collector_workers"):
                load_config(str(path))

    def test_demo_transform_disables_investment_filing_jobs(self):
        from config_loader import _demo_transform

        raw = {
            "collectors": {},
            "processors": {},
            "investment_filings": {
                "enabled": True,
                "schedule": "0 8 * * 1-5",
                "run_on_startup": True,
            },
        }
        with patch.dict(os.environ, {"DEMO_MODE": "1"}, clear=False):
            _demo_transform(raw)
        self.assertFalse(raw["investment_filings"]["enabled"])
        self.assertIsNone(raw["investment_filings"]["schedule"])
        self.assertFalse(raw["investment_filings"]["run_on_startup"])

    def test_loaded_config_supports_existing_call_sites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                extra=(
                    "collectors:\n"
                    "  fred:\n"
                    "    enabled: true\n"
                    "    api_key: real-fred-key\n"
                    "    series:\n"
                    "      - id: DGS10\n"
                    "        frequency: daily\n"
                    "processors:\n"
                    "  briefing:\n"
                    "    enabled: false\n"
                ),
            )
            config = load_config(str(path))
            self.assertEqual(
                config.get("collectors", {}).get("fred", {}).get("enabled"), True
            )
            self.assertEqual(
                config.get("processors", {}).get("briefing", {}).get("enabled"), False
            )
            self.assertEqual(
                config.get("collectors", {}).get("fred", {}).get("series", [])[0]["id"],
                "DGS10",
            )
            self.assertEqual(dict(config["database"])["host"], "localhost")

    def test_version_and_restart_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            operator = tmp / "operator.yaml"
            secrets = tmp / "secrets.env"
            path = tmp / "config.yaml"
            _write_config(path)
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                },
                clear=True,
            ):
                load_config(str(path))
                self.assertIsNotNone(config_version())
                self.assertEqual(config_version(), config_version())
                # Scheduler-fed change (collector toggle) requires restart:
                # APScheduler captured the job definitions at startup.
                operator.write_text("collectors:\n  fred:\n    enabled: false\n")
                reload_config(str(path))
                self.assertTrue(restart_required())
                self.assertIn("collectors", restart_sensitive_changes())
                self.assertNotEqual(config_version(), None)
                # A later live-only change must not erase the pending restart
                # while the restart-sensitive collector delta remains active.
                operator.write_text(
                    "collectors:\n"
                    "  fred:\n"
                    "    enabled: false\n"
                    "logging:\n"
                    "  level: DEBUG\n"
                )
                reload_config(str(path))
                self.assertTrue(restart_required())
                self.assertIn("collectors", restart_sensitive_changes())
                # Restart-sensitive change (database): restart required.
                operator.write_text("database:\n  host: other-host\n")
                reload_config(str(path))
                self.assertTrue(restart_required())
                self.assertIn("database", restart_sensitive_changes())


class FreeCollectorConfigValidationTests(unittest.TestCase):
    """Strict nested config models for the free/public collector ids."""

    PRODUCTION_ENV = {
        "DB_USER": "trading",
        "DB_PASSWORD": "correct-horse-battery-staple",
        "OPENROUTER_API_KEY": "configured-openrouter",
        "TWITTERAPI_KEY": "",
        "DASHBOARD_USER": "operator",
        "DASHBOARD_PASSWORD": "correct-dashboard-password",
        "DEPLOYMENT_MODE": "production",
    }

    def _write_full_config(self, path: Path, extra: str = "") -> None:
        path.write_text(
            "database:\n"
            "  host: localhost\n"
            "  port: 5432\n"
            "  name: test\n"
            "  user: runtime-user\n"
            "  password: correct-horse-battery\n"
            "llm:\n"
            "  api_key: configured-openrouter\n"
            "  models:\n"
            "    default: provider/model\n"
            f"{extra}"
        )

    def test_irrelevant_collector_fields_reject(self):
        """A field valid for another source fails on a free collector."""
        cases = [
            (
                "issuer_news",
                "collectors:\n"
                "  issuer_news:\n"
                "    enabled: true\n"
                '    schedule: "*/30 * * * *"\n'
                "    feeds:\n"
                "      - name: sec\n"
                '        url: "https://www.sec.gov/feed"\n'
                "    symbols: [AAPL]\n",
            ),
            (
                "fred",
                "collectors:\n"
                "  fred:\n"
                "    enabled: false\n"
                "    api_key: real-fred-key\n"
                "    feeds:\n"
                "      - institution: fed\n"
                "        document_type: decision\n"
                '        url: "https://www.federalreserve.gov/feed"\n',
            ),
            (
                "sec_form4",
                "collectors:\n"
                "  sec_form4:\n"
                "    enabled: true\n"
                '    schedule: "5 * * * mon-fri"\n'
                "    issuers:\n"
                '      - cik: "0000320193"\n'
                "        symbol: AAPL\n"
                "    transcription:\n"
                "      model: tiny\n",
            ),
        ]
        for source_id, extra in cases:
            with self.subTest(source_id=source_id):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.yaml"
                    self._write_full_config(path, extra)
                    with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                        with self.assertRaisesRegex(ConfigError, "not applicable"):
                            load_config(str(path))

    def test_include_active_theses_defaults_safe_and_mapping_compatible(self):
        """Absent flag stays False and reads back through the mapping API."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            self._write_full_config(
                path,
                "collectors:\n"
                "  public_equities:\n"
                "    enabled: true\n"
                '    schedule: "30 21 * * 1-5"\n'
                "    symbols: [AAPL]\n",
            )
            with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                config = load_config(str(path))
            equities = config["collectors"]["public_equities"]
            self.assertIs(equities.get("include_active_theses"), False)
            # The untouched default never triggers per-source applicability.
            self.assertNotIn("include_active_theses", equities.model_fields_set)

    def test_include_active_theses_override_accepted_for_public_equities(self):
        """Explicit true opts the active-thesis universe expansion in."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            self._write_full_config(
                path,
                "collectors:\n"
                "  public_equities:\n"
                "    enabled: true\n"
                '    schedule: "30 21 * * 1-5"\n'
                "    symbols: [AAPL]\n"
                "    include_active_theses: true\n",
            )
            with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                config = load_config(str(path))
            equities = config["collectors"]["public_equities"]
            self.assertIs(equities.get("include_active_theses"), True)
            self.assertIn("include_active_theses", equities.model_fields_set)

    def test_include_investment_universe_accepted_only_for_public_equities(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            self._write_full_config(
                path,
                "collectors:\n"
                "  public_equities:\n"
                "    enabled: true\n"
                '    schedule: "30 21 * * 1-5"\n'
                "    symbols: [AAPL]\n"
                "    max_symbols: 400\n"
                "    max_concurrency: 8\n"
                "    include_investment_universe: true\n",
            )
            with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                config = load_config(str(path))
        equities = config["collectors"]["public_equities"]
        self.assertIs(equities.get("include_investment_universe"), True)
        self.assertEqual(equities.get("max_symbols"), 400)
        self.assertEqual(equities.get("max_concurrency"), 8)

    def test_include_active_theses_rejected_on_other_sources(self):
        """The flag is rejected outside bounded market-universe collectors."""
        cases = [
            (
                "issuer_news",
                "collectors:\n"
                "  issuer_news:\n"
                "    enabled: true\n"
                '    schedule: "*/30 * * * *"\n'
                "    include_active_theses: true\n"
                "    feeds:\n"
                "      - name: sec\n"
                '        url: "https://www.sec.gov/feed"\n',
            ),
            (
                "sec_form4",
                "collectors:\n"
                "  sec_form4:\n"
                "    enabled: true\n"
                '    schedule: "5 * * * mon-fri"\n'
                "    include_active_theses: true\n"
                "    issuers:\n"
                '      - cik: "0000320193"\n'
                "        symbol: AAPL\n",
            ),
        ]
        for source_id, extra in cases:
            with self.subTest(source_id=source_id):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.yaml"
                    self._write_full_config(path, extra)
                    with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                        with self.assertRaisesRegex(
                            ConfigError, "include_active_theses"
                        ):
                            load_config(str(path))

    def test_include_active_theses_requires_a_real_boolean(self):
        """Coerced strings/numbers are rejected instead of silently enabling."""
        for raw in ('"true"', '"yes"', "1", "0"):
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.yaml"
                    self._write_full_config(
                        path,
                        "collectors:\n"
                        "  public_equities:\n"
                        "    enabled: true\n"
                        '    schedule: "30 21 * * 1-5"\n'
                        "    symbols: [AAPL]\n"
                        f"    include_active_theses: {raw}\n",
                    )
                    with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                        with self.assertRaises(ConfigError):
                            load_config(str(path))

    def test_public_equities_max_symbols_capped_at_collector_hard_max(self):
        """A cap the collector would reject at runtime fails at startup."""
        for raw, accepted in (("400", True), ("401", False), ("1000", False)):
            with self.subTest(max_symbols=raw):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.yaml"
                    self._write_full_config(
                        path,
                        "collectors:\n"
                        "  public_equities:\n"
                        "    enabled: true\n"
                        '    schedule: "30 21 * * 1-5"\n'
                        "    symbols: [AAPL]\n"
                        f"    max_symbols: {raw}\n",
                    )
                    with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                        if accepted:
                            config = load_config(str(path))
                            self.assertEqual(
                                config["collectors"]["public_equities"].get(
                                    "max_symbols"
                                ),
                                400,
                            )
                        else:
                            with self.assertRaisesRegex(ConfigError, "hard cap"):
                                load_config(str(path))

    def test_public_equities_concurrency_cannot_exceed_runtime_cap(self):
        for raw, accepted in (("16", True), ("17", False), ("64", False)):
            with self.subTest(max_concurrency=raw):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.yaml"
                    self._write_full_config(
                        path,
                        "collectors:\n"
                        "  public_equities:\n"
                        "    enabled: true\n"
                        "    symbols: [AAPL]\n"
                        f"    max_concurrency: {raw}\n",
                    )
                    with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                        if accepted:
                            config = load_config(str(path))
                            self.assertEqual(
                                config["collectors"]["public_equities"].get(
                                    "max_concurrency"
                                ),
                                16,
                            )
                        else:
                            with self.assertRaisesRegex(ConfigError, "hard cap"):
                                load_config(str(path))

    def test_max_symbols_high_bound_stays_public_equities_specific(self):
        """The hard cap does not leak to other sources sharing the field."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            self._write_full_config(
                path,
                "collectors:\n"
                "  cboe_options:\n"
                "    enabled: true\n"
                '    schedule: "*/30 13-20 * * 1-5"\n'
                "    symbols: [SPY]\n"
                "    max_symbols: 1000\n",
            )
            with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                config = load_config(str(path))
            self.assertEqual(
                config["collectors"]["cboe_options"].get("max_symbols"), 1000
            )

    def test_public_equities_symbols_reject_structured_entries(self):
        """FINRA-style structured symbols fail startup, alone or mixed."""
        cases = [
            (
                "structured-only",
                "collectors:\n"
                "  public_equities:\n"
                "    enabled: true\n"
                '    schedule: "30 21 * * 1-5"\n'
                "    symbols:\n"
                "      - symbol: AAPL\n"
                "        assets: [AAPL]\n",
            ),
            (
                "mixed",
                "collectors:\n"
                "  public_equities:\n"
                "    enabled: true\n"
                '    schedule: "30 21 * * 1-5"\n'
                "    symbols:\n"
                "      - AAPL\n"
                "      - symbol: MSFT\n"
                "        assets: [MSFT]\n",
            ),
        ]
        for label, extra in cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.yaml"
                    self._write_full_config(path, extra)
                    with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                        with self.assertRaisesRegex(
                            ConfigError, "must contain only strings"
                        ):
                            load_config(str(path))

    def test_public_equities_string_symbols_load_and_stay_mapping_compatible(self):
        """Plain-string symbols pass validation and read back as strings."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            self._write_full_config(
                path,
                "collectors:\n"
                "  public_equities:\n"
                "    enabled: true\n"
                '    schedule: "30 21 * * 1-5"\n'
                "    symbols: [AAPL, MSFT]\n",
            )
            with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                config = load_config(str(path))
            equities = config["collectors"]["public_equities"]
            self.assertEqual(equities.get("symbols"), ["AAPL", "MSFT"])

    def test_public_equities_symbols_reject_invalid_strings(self):
        """Symbols outside the collector grammar fail startup."""
        cases = [
            ("blank", ""),
            ("whitespace-only", "   "),
            ("bad-char", "AAPL!"),
            ("slash", "AAPL/"),
            ("leading-dash", "-AAPL"),
            ("too-long", "A" * 21),
        ]
        for label, symbol in cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.yaml"
                    self._write_full_config(
                        path,
                        "collectors:\n"
                        "  public_equities:\n"
                        "    enabled: true\n"
                        '    schedule: "30 21 * * 1-5"\n'
                        f"    symbols: [{symbol!r}]\n",
                    )
                    with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                        with self.assertRaisesRegex(ConfigError, "symbol grammar"):
                            load_config(str(path))

    def test_public_equities_symbols_accept_case_and_whitespace_variants(self):
        """Grammar-valid case/whitespace variants pass, stored verbatim."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            self._write_full_config(
                path,
                "collectors:\n"
                "  public_equities:\n"
                "    enabled: true\n"
                '    schedule: "30 21 * * 1-5"\n'
                "    symbols: [aapl, ' MSFT ', brk.b, "
                f"{('A' * 20)!r}]\n",
            )
            with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                config = load_config(str(path))
            equities = config["collectors"]["public_equities"]
            # Stored verbatim; the collector canonicalizes at collection.
            self.assertEqual(
                equities.get("symbols"),
                ["aapl", " MSFT ", "brk.b", "A" * 20],
            )

    def test_public_equities_symbols_reject_canonical_duplicates(self):
        """Duplicates are detected after trim + uppercase canonicalization."""
        cases = [
            ("exact", "[AAPL, AAPL]"),
            ("case", "[AAPL, aapl]"),
            ("whitespace", "[AAPL, ' AAPL ']"),
            ("case-and-whitespace", "[' aapl ', 'AAPL ']"),
        ]
        for label, symbols in cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.yaml"
                    self._write_full_config(
                        path,
                        "collectors:\n"
                        "  public_equities:\n"
                        "    enabled: true\n"
                        '    schedule: "30 21 * * 1-5"\n'
                        f"    symbols: {symbols}\n",
                    )
                    with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                        with self.assertRaisesRegex(ConfigError, "duplicates"):
                            load_config(str(path))

    def test_public_equities_symbol_count_capped_by_max_symbols(self):
        """More configured symbols than the section cap fail startup."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            self._write_full_config(
                path,
                "collectors:\n"
                "  public_equities:\n"
                "    enabled: true\n"
                '    schedule: "30 21 * * 1-5"\n'
                "    symbols: [AAPL, MSFT, NVDA]\n"
                "    max_symbols: 2\n",
            )
            with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                with self.assertRaisesRegex(
                    ConfigError, "exceeds the configured limit"
                ):
                    load_config(str(path))
        # At the boundary, exactly max_symbols symbols load.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            self._write_full_config(
                path,
                "collectors:\n"
                "  public_equities:\n"
                "    enabled: true\n"
                '    schedule: "30 21 * * 1-5"\n'
                "    symbols: [AAPL, MSFT]\n"
                "    max_symbols: 2\n",
            )
            with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                config = load_config(str(path))
            self.assertEqual(
                config["collectors"]["public_equities"].get("symbols"),
                ["AAPL", "MSFT"],
            )

    def test_finra_structured_symbols_round_trip_unchanged(self):
        """FINRA structured symbol objects validate and round-trip verbatim."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            self._write_full_config(
                path,
                "collectors:\n"
                "  finra_short_volume:\n"
                "    enabled: true\n"
                '    schedule: "15 23 * * 1-5"\n'
                "    symbols:\n"
                "      - symbol: AAPL\n"
                "        assets: [AAPL, AAPL.P]\n"
                "      - symbol: MSFT\n"
                "        assets: [MSFT]\n",
            )
            with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                config = load_config(str(path))
            finra = config["collectors"]["finra_short_volume"]
            self.assertEqual(len(finra.get("symbols")), 2)
            first = finra.get("symbols")[0]
            self.assertEqual(first.get("symbol"), "AAPL")
            self.assertEqual(first.get("assets"), ["AAPL", "AAPL.P"])
            self.assertEqual(finra.get("symbols")[1].get("symbol"), "MSFT")
            self.assertEqual(finra.get("symbols")[1].get("assets"), ["MSFT"])

    def test_finra_short_volume_structured_symbols_stay_valid(self):
        """FINRA structured symbols remain valid for finra_short_volume."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            self._write_full_config(
                path,
                "collectors:\n"
                "  finra_short_volume:\n"
                "    enabled: true\n"
                '    schedule: "15 23 * * 1-5"\n'
                "    symbols:\n"
                "      - symbol: AAPL\n"
                "        assets: [AAPL]\n",
            )
            with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                config = load_config(str(path))
            finra = config["collectors"]["finra_short_volume"]
            self.assertEqual(finra.get("symbols")[0].get("symbol"), "AAPL")
            self.assertEqual(finra.get("symbols")[0].get("assets"), ["AAPL"])

    def test_nested_invalid_bounds_reject(self):
        """Out-of-range nested values fail before they reach a collector."""
        cases = [
            (
                "cboe_options",
                "max_response_bytes",
                "collectors:\n"
                "  cboe_options:\n"
                "    enabled: true\n"
                '    schedule: "*/30 13-20 * * 1-5"\n'
                "    symbols: [SPY]\n"
                "    max_response_bytes: 100\n",
            ),
            (
                "issuer_news",
                "max_items",
                "collectors:\n"
                "  issuer_news:\n"
                "    enabled: true\n"
                '    schedule: "*/30 * * * *"\n'
                "    feeds:\n"
                "      - name: sec\n"
                '        url: "https://www.sec.gov/feed"\n'
                "        max_items: 0\n",
            ),
            (
                "issuer_transcripts",
                "max_issuers",
                "collectors:\n"
                "  issuer_transcripts:\n"
                "    enabled: true\n"
                '    schedule: "0 */6 * * *"\n'
                "    max_issuers: 0\n"
                "    issuers:\n"
                "      - institution: Apple\n"
                '        url: "https://investor.apple.com/events/default.aspx"\n',
            ),
            (
                "finra_short_volume",
                "max_file_bytes",
                "collectors:\n"
                "  finra_short_volume:\n"
                "    enabled: true\n"
                '    schedule: "15 23 * * 1-5"\n'
                "    symbols: [AAPL]\n"
                "    max_file_bytes: 50000\n",
            ),
            (
                "public_equities",
                "interval",
                "collectors:\n"
                "  public_equities:\n"
                "    enabled: true\n"
                '    schedule: "30 21 * * 1-5"\n'
                "    symbols: [AAPL]\n"
                '    interval: "5m"\n',
            ),
            (
                "sec_form4",
                "max_filings_per_issuer",
                "collectors:\n"
                "  sec_form4:\n"
                "    enabled: true\n"
                '    schedule: "5 * * * mon-fri"\n'
                "    max_filings_per_issuer: 0\n"
                "    issuers:\n"
                '      - cik: "0000320193"\n'
                "        symbol: AAPL\n",
            ),
        ]
        for source_id, fragment, extra in cases:
            with self.subTest(source_id=source_id, fragment=fragment):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.yaml"
                    self._write_full_config(path, extra)
                    with self.assertRaisesRegex(ConfigError, fragment):
                        load_config(str(path))

    def test_free_collector_sections_stay_mapping_compatible(self):
        """Loaded sections keep the .get() access collector code relies on."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            self._write_full_config(
                path,
                "collectors:\n"
                "  issuer_news:\n"
                "    enabled: true\n"
                '    schedule: "*/30 * * * *"\n'
                '    state_path: "/tmp/issuer_news.json"\n'
                "    feeds:\n"
                "      - name: sec_current_8k\n"
                "        kind: feed\n"
                '        url: "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=40&output=atom"\n'
                "        max_items: 40\n"
                "  issuer_transcripts:\n"
                "    enabled: true\n"
                '    schedule: "0 */6 * * *"\n'
                "    transcription:\n"
                "      model: tiny\n"
                "      beam_size: 2\n"
                "    issuers:\n"
                "      - kind: q4_events\n"
                "        institution: Amazon\n"
                "        ticker: AMZN\n"
                '        url: "https://ir.aboutamazon.com/feed/Event.svc/GetEventList"\n'
                "  public_equities:\n"
                "    enabled: true\n"
                '    schedule: "30 21 * * 1-5"\n'
                "    symbols: [AAPL, MSFT]\n"
                "    max_symbols: 25\n"
                '    range: "1y"\n'
                '    interval: "1d"\n'
                "  sec_form4:\n"
                "    enabled: true\n"
                '    schedule: "5 * * * mon-fri"\n'
                "    issuers:\n"
                '      - cik: "0000320193"\n'
                "        symbol: AAPL\n"
                "  finra_short_volume:\n"
                "    enabled: true\n"
                '    schedule: "15 23 * * 1-5"\n'
                "    symbols:\n"
                "      - symbol: AAPL\n"
                "        assets: [AAPL]\n"
                "  cboe_options:\n"
                "    enabled: true\n"
                '    schedule: "*/30 13-20 * * 1-5"\n'
                "    symbols: [SPY, QQQ]\n"
                '    source_timezone: "America/Chicago"\n',
            )
            config = load_config(str(path))
            issuer_news = config["collectors"]["issuer_news"]
            self.assertEqual(issuer_news.get("schedule"), "*/30 * * * *")
            self.assertEqual(issuer_news.get("state_path"), "/tmp/issuer_news.json")
            feed = issuer_news.get("feeds")[0]
            self.assertEqual(feed.get("name"), "sec_current_8k")
            self.assertTrue(feed.get("enabled", True))
            self.assertEqual(feed.get("max_items"), 40)
            transcripts = config["collectors"]["issuer_transcripts"]
            self.assertEqual(transcripts.get("transcription").get("model"), "tiny")
            self.assertEqual(transcripts.get("issuers")[0].get("ticker"), "AMZN")
            self.assertEqual(transcripts.get("issuers")[0].get("kind"), "q4_events")
            equities = config["collectors"]["public_equities"]
            self.assertEqual(equities.get("symbols"), ["AAPL", "MSFT"])
            self.assertEqual(equities.get("range"), "1y")
            sec = config["collectors"]["sec_form4"]
            self.assertEqual(sec.get("issuers")[0].get("cik"), "0000320193")
            finra = config["collectors"]["finra_short_volume"]
            self.assertEqual(finra.get("symbols")[0].get("symbol"), "AAPL")
            self.assertEqual(finra.get("symbols")[0].get("assets"), ["AAPL"])
            cboe = config["collectors"]["cboe_options"]
            self.assertEqual(cboe.get("symbols"), ["SPY", "QQQ"])
            self.assertEqual(cboe.get("source_timezone"), "America/Chicago")

    def test_checked_in_config_parses_with_registry_equality(self):
        """The checked-in config is valid, fully known, and dispatchable."""
        from collectors import STANDALONE_COLLECTORS, get_all_collectors, get_collector
        from contracts.runtime_config import KNOWN_COLLECTORS
        from schedules import build_cron_trigger

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
            config = load_config(str(config_path))

        executable = set(get_all_collectors()) | set(STANDALONE_COLLECTORS)
        self.assertEqual(set(KNOWN_COLLECTORS), executable)
        self.assertEqual(set(config.collectors), set(KNOWN_COLLECTORS))
        for source_id in sorted(KNOWN_COLLECTORS):
            with self.subTest(dispatch=source_id):
                self.assertEqual(get_collector(source_id).source_id, source_id)
        for source_id, collector_config in config.collectors.items():
            if not collector_config.enabled:
                continue
            schedule = collector_config.get("schedule")
            self.assertIsNotNone(schedule, source_id)
            build_cron_trigger(schedule)  # raises on a malformed cron
        for source_id in (
            "issuer_news",
            "issuer_transcripts",
            "public_equities",
            "sec_form4",
            "finra_short_volume",
            "cboe_options",
            "company_expectations",
        ):
            with self.subTest(enabled=source_id):
                self.assertTrue(config.collectors[source_id].enabled)

    def test_checked_in_thesis_autonomy_profile_is_enabled_and_bounded(self):
        from schedules import build_cron_trigger

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
            config = load_config(str(config_path))

        autonomy = config.thesis_autonomy
        self.assertTrue(autonomy.enabled)
        self.assertTrue(autonomy.schedule_enabled)
        build_cron_trigger(autonomy.schedule)  # raises on a malformed cron
        self.assertEqual(autonomy.maximum_evidence, 400)
        self.assertEqual(autonomy.maximum_promoted, 4)
        self.assertEqual(autonomy.maximum_challenges_per_run, 4)
        self.assertEqual(autonomy.event_debounce_minutes, 180)
        self.assertEqual(autonomy.maximum_event_runs_per_day, 2)
        self.assertEqual(
            autonomy.model_override, "nvidia/nemotron-3-super-120b-a12b:free"
        )
        self.assertEqual(autonomy.model_budget_usd_per_run, 0.75)
        self.assertLessEqual(
            autonomy.model_budget_usd_per_run, config.budgets.daily_llm_usd
        )
        # Desk llm policy is explicit, never a silent fallback.
        self.assertEqual(config.llm.max_output_tokens["thesis_autonomy"], 16384)
        self.assertTrue(config.llm.structured_response["thesis_autonomy"])
        self.assertFalse(config.llm.require_parameters["thesis_autonomy"])
        self.assertEqual(config.llm.max_prices["thesis_autonomy"]["completion"], 3.5)
        # Event pipeline sources cover every free/public publisher.
        self.assertEqual(
            set(config.event_pipeline.sources),
            {
                "fred",
                "oanda",
                "issuer_news",
                "issuer_transcripts",
                "public_equities",
                "sec_form4",
                "cftc",
                "finra_short_volume",
                "cboe_options",
                "company_expectations",
            },
        )


class ConfigStoreConcurrencyTests(unittest.TestCase):
    def test_store_serializes_concurrent_reload_state_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.yaml"
            operator = root / "operator.yaml"
            secrets = root / "secrets.env"
            _write_config(path)
            store = ConfigStore()
            start = threading.Barrier(3)
            counter_lock = threading.Lock()
            active = 0
            max_active = 0
            errors = []

            def parse(candidate: str):
                nonlocal active, max_active
                with counter_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.05)
                    return yaml.safe_load(Path(candidate).read_text())
                finally:
                    with counter_lock:
                        active -= 1

            def run():
                start.wait()
                try:
                    store.reload(
                        config_path=str(path),
                        operator_path=str(operator),
                        secrets_path=str(secrets),
                        parse=parse,
                    )
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(timeout=2)

            self.assertFalse(errors)
            self.assertEqual(max_active, 1)
            self.assertEqual(store.status()["ordinal"], 2)

    def test_committed_paths_lock_root_before_resolving_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            version = state / "versions" / "v1"
            version.mkdir(parents=True)
            (version / "operator.yaml").write_text("")
            (version / "secrets.env").write_text("")
            (state / "current").symlink_to("versions/v1")
            (state / "operator.yaml").symlink_to("current/operator.yaml")
            (state / "secrets.env").symlink_to("current/secrets.env")

            with committed_config_paths(
                str(state / "operator.yaml"), str(state / "secrets.env")
            ) as paths:
                self.assertEqual(Path(paths[0]).parent, version)
                self.assertEqual(Path(paths[1]).parent, version)

            self.assertTrue((state / ".setup.lock").exists())
            self.assertFalse((version / ".setup.lock").exists())


class QualitySemanticsTests(unittest.TestCase):
    def test_missing_required_check_is_not_healthy(self):
        required = {"fred_DGS10_freshness", "fred_DGS10_gaps"}
        results = {"fred_DGS10_freshness": {"healthy": True, "detail": "fresh"}}
        self.assertEqual(evaluate_quality(results, required), "unknown")

    def test_empty_required_registry_is_unknown(self):
        self.assertEqual(evaluate_quality({}, set()), "unknown")
        self.assertEqual(evaluate_quality({"a": {"healthy": True}}, set()), "degraded")

    def test_unhealthy_check_degrades_overall(self):
        results = {"fred_DGS10_freshness": {"healthy": False, "detail": "stale"}}
        self.assertEqual(evaluate_quality(results, set(results)), "degraded")

    def test_all_required_present_and_healthy_is_healthy(self):
        results = {"fred_DGS10_freshness": {"healthy": True}}
        self.assertEqual(evaluate_quality(results, set(results)), "healthy")

    def test_required_checks_follow_enabled_sources(self):
        config = {
            "collectors": {
                "fred": {
                    "enabled": True,
                    "series": [{"id": "DGS10", "frequency": "daily"}],
                },
                "oanda": {"enabled": False},
            }
        }
        required = required_quality_checks(config)
        self.assertIn("fred_DGS10_freshness", required)
        self.assertIn("fred_DGS10_gaps", required)
        self.assertIn("fred_DGS10_anomalies", required)
        self.assertIn("forex_factory_freshness", required)  # default enabled
        self.assertNotIn("oanda_freshness", required)  # disabled -> optional

    def test_disabled_fred_without_series_falls_back_to_fixed_checks(self):
        config = {"collectors": {"fred": {"enabled": True, "series": []}}}
        required = required_quality_checks(config)
        self.assertIn("fred_freshness", required)

    def test_loaded_config_feeds_real_budget_and_quality_consumers(self):
        """A validated snapshot must satisfy real config consumers end to end."""
        from budgets import _reservation_policy, get_budget_config

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            operator = tmp / "operator.yaml"
            secrets = tmp / "secrets.env"
            path = tmp / "config.yaml"
            _write_config(
                path,
                extra=(
                    "budgets:\n"
                    "  daily_llm_usd: 4.0\n"
                    "  warn_at_pct: 75\n"
                    "  reservation_estimate_usd: 0.10\n"
                    "  reservation_ttl_seconds: 900\n"
                    "collectors:\n"
                    "  fred:\n"
                    "    enabled: true\n"
                    "    api_key: real-fred-key\n"
                    "    series:\n"
                    "      - id: DGS10\n"
                    "        frequency: daily\n"
                    "  oanda:\n"
                    "    enabled: false\n"
                ),
            )
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                },
                clear=True,
            ):
                config = load_config(str(path))
                self.assertEqual(get_budget_config(config), (4.0, 75.0))
                estimate, ttl = _reservation_policy(config, "briefing")
                self.assertEqual((estimate, ttl), (0.10, 900.0))
                required = required_quality_checks(config)
                self.assertIn("fred_DGS10_freshness", required)
                self.assertNotIn("oanda_freshness", required)

    def test_loaded_config_without_pricing_fails_closed_for_paid(self):
        """A validated config must never manufacture paid pricing.

        The runtime schema has no silent per-call estimate default: when
        ``budgets.reservation_estimate_usd`` is absent, a paid model fails
        closed at admission (``BudgetUnavailable``) instead of being priced
        at a guessed 0.05 by the config layer.
        """
        from budgets import (
            BudgetUnavailable,
            _reservation_policy,
            enforce_budget,
            get_budget_config,
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            operator = tmp / "operator.yaml"
            secrets = tmp / "secrets.env"
            path = tmp / "config.yaml"
            _write_config(
                path,
                extra=(
                    "budgets:\n"
                    "  daily_llm_usd: 4.0\n"
                    "  warn_at_pct: 75\n"
                    "collectors:\n"
                    "  fred:\n"
                    "    enabled: true\n"
                    "    api_key: real-fred-key\n"
                    "    series:\n"
                    "      - id: DGS10\n"
                    "        frequency: daily\n"
                ),
            )
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                },
                clear=True,
            ):
                config = load_config(str(path))
                # No pricing materialized by the schema: the paid policy
                # raises instead of guessing.
                with self.assertRaisesRegex(ValueError, "no configured pricing"):
                    _reservation_policy(config, "briefing")
                # The enforce path maps that to the fail-closed block and
                # never reaches a reservation.
                with patch("budgets._reserve_budget_quota") as reserve:
                    with self.assertRaises(BudgetUnavailable):
                        enforce_budget(config, "briefing")
                reserve.assert_not_called()
                # The daily cap itself is still readable.
                self.assertEqual(get_budget_config(config), (4.0, 75.0))

    def test_invalid_reload_retains_prior_snapshot_and_reports_status(self):
        """A rejected reload keeps the last valid config and never 500s."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            operator = tmp / "operator.yaml"
            secrets = tmp / "secrets.env"
            path = tmp / "config.yaml"
            _write_config(path)
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                },
                clear=True,
            ):
                config = load_config(str(path))
                version_before = config_version()
                operator.write_text("collectors:\n  fred:\n    enabled: [broken\n")
                self.assertIs(reload_config(str(path)), config)
                # Prior snapshot retained; ordinary consumers do not raise.
                self.assertEqual(config_version(), version_before)
                self.assertIs(load_config(str(path)), config)
                self.assertEqual(restart_sensitive_changes(), ["reload_failed"])
                self.assertTrue(restart_required())
                from config_loader import config_status

                status = config_status()
                self.assertTrue(status["last_reload"]["failed"])
                self.assertIn("YAML parse error", status["last_reload"]["error"])
                # Repairing the source clears the rejection and applies live.
                operator.write_text("")
                reload_config(str(path))
                self.assertEqual(restart_sensitive_changes(), [])

    def test_reload_forces_environment_only_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operator = root / "operator.yaml"
            secrets = root / "secrets.env"
            path = root / "config.yaml"
            _write_config(
                path,
                extra=("llm:\n  api_key: k\n  models:\n    default: ${MODEL_NAME}\n"),
            )
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                    "MODEL_NAME": "provider/model-a",
                },
                clear=True,
            ):
                first = reload_config(str(path))
                os.environ["MODEL_NAME"] = "provider/model-b"
                self.assertIs(load_config(str(path)), first)
                second = reload_config(str(path))
                self.assertEqual(second.llm.models["default"], "provider/model-b")
                self.assertIsNot(second, first)

    def test_run_quality_checks_skips_disabled_sources(self):
        with patch("data_quality.get_session"):
            from data_quality import run_quality_checks

            results = run_quality_checks(
                {
                    "collectors": {
                        "fred": {"enabled": False, "series": []},
                        "forex_factory": {"enabled": False},
                        "oanda": {"enabled": False},
                    }
                }
            )
        self.assertEqual(results, {})


class LiveAndReadyEndpointTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from main import app

        config = {
            "demo": {"enabled": False},
            "collectors": {
                "fred": {"enabled": False},
                "forex_factory": {"enabled": False},
                "oanda": {"enabled": False},
            },
            "event_pipeline": {
                "enabled": False,
                "outbox_worker_enabled": False,
                "jobs": {"enabled": False},
            },
        }
        self.config_patch = patch("main._get_config", return_value=config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.client = TestClient(app, headers=INTERNAL_AUTH)

    def _healthy_heartbeat(self, role: str) -> dict:
        from datetime import UTC, datetime, timedelta

        status = "running" if role != "quotes" else "connected"
        return {
            "role": role,
            "status": status,
            "last_heartbeat_at": datetime.now(UTC) - timedelta(seconds=5),
            "detail": {},
        }

    @patch("main.fresh_role_heartbeats")
    @patch("main.check_connection", return_value=True)
    def test_live_returns_200(self, _db, _heartbeat):
        response = self.client.get("/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    @patch("main.fresh_role_heartbeats")
    @patch("main.check_connection", return_value=True)
    def test_ready_returns_200_when_database_and_roles_ok(self, _db, heartbeat):
        heartbeat.side_effect = lambda config, role: [self._healthy_heartbeat(role)]
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["dependencies"]["database"], "ok")
        self.assertIn("api", response.json()["dependencies"]["roles"]["required"])

    @patch("main.fresh_role_heartbeats", return_value=[])
    @patch("main.check_connection", return_value=True)
    def test_ready_returns_503_when_required_role_missing(self, _db, _heartbeat):
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unready")
        unhealthy = response.json()["dependencies"]["roles"]["unhealthy"]
        self.assertIn("api", unhealthy)

    @patch("main.fresh_role_heartbeats")
    @patch("main.check_connection", return_value=False)
    def test_ready_returns_503_when_database_unreachable(self, _db, _heartbeat):
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unready")


if __name__ == "__main__":
    unittest.main()
