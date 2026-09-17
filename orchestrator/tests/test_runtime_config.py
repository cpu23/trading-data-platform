import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DEPLOYMENT_MODE", "test")

from data_quality import evaluate_quality, required_quality_checks
from research_intelligence.config import ResearchSettings

from contracts.runtime_config import (  # noqa: E402
    ConfigError,
    ResearchIntelligenceConfig,
    ResearchStageConfig,
    RuntimeConfigLoader,
    apply_demo_transform,
    committed_config_paths,
)


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


def _load_local(path: Path):
    """Load an isolated sample without replacing the process shared loader."""
    return RuntimeConfigLoader().load(str(path))


class ConfigLoaderValidationTests(unittest.TestCase):
    def test_unknown_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path, extra="mystery_section: 1\n")
            with self.assertRaises(ConfigError):
                _load_local(path)

    def test_failed_initial_candidate_does_not_bind_loader_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / "bad.yaml"
            good = root / "good.yaml"
            bad.write_text("database:\n  host: [broken\n")
            _write_config(good)
            loader = RuntimeConfigLoader()
            with self.assertRaises(ConfigError):
                loader.load(str(bad))
            loaded = loader.load(str(good))

            self.assertIs(loader.load(str(good)), loaded)
            with self.assertRaisesRegex(ConfigError, "already bound"):
                loader.load(str(bad))

    def test_semantic_validation_reports_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path, extra="orchestration:\n  collector_workers: 0\n")
            with self.assertRaisesRegex(ConfigError, "collector_workers"):
                _load_local(path)

    def test_demo_transform_disables_all_external_scheduled_work(self):
        raw = {
            "collectors": {
                "fred": {
                    "enabled": True,
                    "schedule": "0 6 * * *",
                    "api_key": "dummy",
                },
            },
            "processors": {
                "briefing": {
                    "enabled": True,
                    "schedule": "5 7 * * *",
                },
            },
            "reuters": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "15 */2 * * *",
            },
            "kobeissi": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "20 */6 * * *",
            },
            "research_intelligence": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "15 8 * * 1-5",
            },
            "thesis_autonomy": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "0 2 * * *",
            },
            "investment_filings": {
                "enabled": True,
                "schedule": "0 8 * * 1-5",
                "run_on_startup": True,
                "auto_analyze": True,
            },
        }
        with patch.dict(os.environ, {"DEMO_MODE": "1"}, clear=False):
            apply_demo_transform(raw)
        self.assertTrue(raw["demo"]["enabled"])
        self.assertFalse(raw["collectors"]["fred"]["enabled"])
        self.assertFalse(raw["processors"]["briefing"]["enabled"])
        self.assertFalse(raw["reuters"]["enabled"])
        self.assertFalse(raw["reuters"]["schedule_enabled"])
        self.assertFalse(raw["kobeissi"]["enabled"])
        self.assertFalse(raw["kobeissi"]["schedule_enabled"])
        self.assertFalse(raw["research_intelligence"]["schedule_enabled"])
        self.assertFalse(raw["thesis_autonomy"]["schedule_enabled"])
        self.assertFalse(raw["investment_filings"]["enabled"])
        self.assertIsNone(raw["investment_filings"]["schedule"])
        self.assertFalse(raw["investment_filings"]["run_on_startup"])

    def test_demo_transformed_opted_in_config_schedules_no_external_calls(self):
        from scheduler import scheduler_status, start_scheduler, stop_scheduler

        raw = {
            "collectors": {
                "fred": {
                    "enabled": True,
                    "schedule": "0 6 * * *",
                },
            },
            "processors": {
                "briefing": {
                    "enabled": True,
                    "schedule": "5 7 * * *",
                },
            },
            "reuters": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "15 */2 * * *",
            },
            "research_intelligence": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "15 8 * * 1-5",
            },
            "thesis_autonomy": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "0 2 * * *",
            },
            "investment_filings": {
                "enabled": True,
                "schedule": "0 8 * * 1-5",
                "run_on_startup": True,
            },
        }
        with patch.dict(os.environ, {"DEMO_MODE": "1"}, clear=False):
            apply_demo_transform(raw)

        self.addCleanup(stop_scheduler)
        start_scheduler(raw)
        status = scheduler_status()
        self.assertEqual(status["jobs"], [])

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
            config = _load_local(path)
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

    def test_market_intelligence_processor_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                extra=("processors:\n  market_intelligence:\n    enabled: true\n"),
            )
            with self.assertRaisesRegex(ConfigError, "unknown processor id"):
                _load_local(path)

    def test_briefing_processor_accepts_asset_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                extra=(
                    "processors:\n"
                    "  briefing:\n"
                    "    enabled: true\n"
                    "    asset_context:\n"
                    "      EURUSD:\n"
                    "        channels: [monetary policy]\n"
                ),
            )
            config = _load_local(path)
            self.assertEqual(
                config.processors["briefing"].asset_context["EURUSD"].channels,
                ["monetary policy"],
            )

    def test_intelligence_roles_rejected_under_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                extra=(
                    "llm:\n"
                    "  intelligence_roles:\n"
                    "    analyst:\n"
                    "      reasoning_effort: low\n"
                ),
            )
            with self.assertRaises(ConfigError):
                _load_local(path)

    def test_version_and_restart_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            operator = tmp / "operator.yaml"
            secrets = tmp / "secrets.env"
            path = tmp / "config.yaml"
            _write_config(
                path,
                extra="collectors:\n  fred:\n    enabled: true\n    api_key: configured-fred\n",
            )
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                },
                clear=True,
            ):
                loader = RuntimeConfigLoader()
                config = loader.load(str(path))
                self.assertIsNotNone(loader.config_version())
                self.assertEqual(loader.config_version(), loader.config_version())
                # Committed changes are visible to status, never hot-applied.
                operator.write_text("collectors:\n  fred:\n    enabled: false\n")
                self.assertIs(loader.load(str(path)), config)
                self.assertTrue(loader.restart_required())
                self.assertIn("collectors", loader.restart_sensitive_changes())
                self.assertNotEqual(loader.config_version(), None)
                self.assertTrue(config["collectors"]["fred"]["enabled"])
                # Further edits retain the same startup snapshot and pending
                # restart requirement.
                operator.write_text(
                    "collectors:\n"
                    "  fred:\n"
                    "    enabled: false\n"
                    "logging:\n"
                    "  level: DEBUG\n"
                )
                self.assertIs(loader.load(str(path)), config)
                self.assertTrue(loader.restart_required())
                self.assertIn("collectors", loader.restart_sensitive_changes())


class FreeCollectorConfigValidationTests(unittest.TestCase):
    """Strict nested config models for the free/public collector ids."""

    def _load_local(self, path: Path):
        return RuntimeConfigLoader().load(str(path))

    PRODUCTION_ENV = {
        "DB_USER": "trading",
        "DB_PASSWORD": "correct-horse-battery-staple",
        "OPENROUTER_API_KEY": "configured-openrouter",
        "TWITTERAPI_KEY": "",
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
                "    symbols: [AAPL]\n",
            ),
        ]
        for source_id, extra in cases:
            with self.subTest(source_id=source_id):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.yaml"
                    self._write_full_config(path, extra)
                    with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
                        with self.assertRaisesRegex(ConfigError, "not applicable"):
                            self._load_local(path)

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
                config = self._load_local(path)
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
                config = self._load_local(path)
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
                config = self._load_local(path)
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
                            self._load_local(path)

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
                            self._load_local(path)

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
                            config = self._load_local(path)
                            self.assertEqual(
                                config["collectors"]["public_equities"].get(
                                    "max_symbols"
                                ),
                                400,
                            )
                        else:
                            with self.assertRaisesRegex(ConfigError, "hard cap"):
                                self._load_local(path)

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
                            config = self._load_local(path)
                            self.assertEqual(
                                config["collectors"]["public_equities"].get(
                                    "max_concurrency"
                                ),
                                16,
                            )
                        else:
                            with self.assertRaisesRegex(ConfigError, "hard cap"):
                                self._load_local(path)

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
                config = self._load_local(path)
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
                            self._load_local(path)

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
                config = self._load_local(path)
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
                            self._load_local(path)

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
                config = self._load_local(path)
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
                            self._load_local(path)

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
                    self._load_local(path)
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
                config = self._load_local(path)
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
                config = self._load_local(path)
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
                config = self._load_local(path)
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
                        self._load_local(path)

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
            config = self._load_local(path)
            issuer_news = config["collectors"]["issuer_news"]
            self.assertEqual(issuer_news.get("schedule"), "*/30 * * * *")
            self.assertEqual(issuer_news.get("state_path"), "/tmp/issuer_news.json")
            feed = issuer_news.get("feeds")[0]
            self.assertEqual(feed.get("name"), "sec_current_8k")
            self.assertTrue(feed.get("enabled", True))
            self.assertEqual(feed.get("max_items"), 40)
            transcripts = config["collectors"]["issuer_transcripts"]
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
        from processors import get_all_processors
        from schedules import build_cron_trigger

        from contracts.runtime_config import KNOWN_COLLECTORS, KNOWN_PROCESSORS

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
            config = self._load_local(config_path)

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

        self.assertEqual(set(KNOWN_PROCESSORS), set(get_all_processors()))
        self.assertEqual(set(config.processors), set(KNOWN_PROCESSORS))

    def test_checked_in_thesis_autonomy_profile_is_enabled_and_bounded(self):
        from schedules import build_cron_trigger

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        with patch.dict(os.environ, self.PRODUCTION_ENV, clear=False):
            config = self._load_local(config_path)

        autonomy = config.thesis_autonomy
        self.assertTrue(autonomy.enabled)
        self.assertFalse(autonomy.schedule_enabled)
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


class RuntimeConfigLoaderConcurrencyTests(unittest.TestCase):
    def test_candidate_validation_finishes_while_reader_waits_for_setup_commit(self):
        import fcntl
        from contextlib import contextmanager

        from contracts import runtime_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path)
            loader = RuntimeConfigLoader()
            reader_waiting = threading.Event()
            validated = threading.Event()
            errors = []
            shared_lock = runtime_config.shared_state_lock

            @contextmanager
            def observed_shared_lock(lock_path):
                reader_waiting.set()
                with shared_lock(lock_path):
                    yield

            def read_config():
                try:
                    loader.load(str(path))
                except Exception as exc:
                    errors.append(exc)

            def validate():
                try:
                    loader.validate_candidate("{}", "", str(path))
                    validated.set()
                except Exception as exc:
                    errors.append(exc)

            reader = threading.Thread(target=read_config, daemon=True)
            writer = threading.Thread(target=validate, daemon=True)
            with (
                patch.dict(
                    os.environ,
                    {
                        "OPERATOR_CONFIG": str(Path(tmp) / "operator.yaml"),
                        "SECRETS_FILE": str(Path(tmp) / "secrets.env"),
                    },
                ),
                patch.object(runtime_config, "shared_state_lock", observed_shared_lock),
                (Path(tmp) / ".setup.lock").open("w") as lock_file,
            ):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    reader.start()
                    self.assertTrue(reader_waiting.wait(2))
                    writer.start()
                    completed_under_write_lock = validated.wait(2)
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    reader.join(2)
                    if writer.ident is not None:
                        writer.join(2)

            self.assertFalse(reader.is_alive())
            self.assertFalse(writer.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(
                completed_under_write_lock,
                "candidate validation deadlocked behind a reader awaiting setup commit",
            )

    def test_loader_serializes_concurrent_initial_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path)
            loader = RuntimeConfigLoader()
            start = threading.Barrier(3)
            configs = []
            errors = []
            result_lock = threading.Lock()

            def run():
                start.wait()
                try:
                    config = loader.load(str(path))
                    with result_lock:
                        configs.append(config)
                except Exception as exc:
                    with result_lock:
                        errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(timeout=2)

            self.assertFalse(errors)
            self.assertEqual(len(configs), 2)
            self.assertIs(configs[0], configs[1])
            self.assertEqual(configs[0].database.host, "localhost")
            self.assertEqual(configs[0].database.user, "runtime-user")

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
                config = _load_local(path)
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
                config = _load_local(path)
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

    def test_invalid_saved_candidate_never_replaces_startup_snapshot(self):
        """An invalid commit cannot replace the immutable startup config."""
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
                loader = RuntimeConfigLoader()
                config = loader.load(str(path))
                version_before = loader.config_version()
                operator.write_text("collectors:\n  fred:\n    enabled: [broken\n")
                self.assertIs(loader.load(str(path)), config)
                self.assertEqual(loader.config_version(), version_before)
                self.assertTrue(loader.restart_required())
                self.assertTrue(loader.config_status()["restart_required"])

    def test_environment_changes_do_not_hot_reload_snapshot(self):
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
                loader = RuntimeConfigLoader()
                first = loader.load(str(path))
                os.environ["MODEL_NAME"] = "provider/model-b"
                self.assertIs(loader.load(str(path)), first)
                self.assertIs(loader.load(str(path)), first)
                self.assertEqual(first.llm.models["default"], "provider/model-a")

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


class ResearchIntelligenceConfigTests(unittest.TestCase):
    def test_typed_settings_conversion_preserves_custom_policy(self):
        config = ResearchIntelligenceConfig(
            limits={
                "maximum_candidate_evidence": 120,
                "maximum_macro_evidence": 32,
                "maximum_market_drivers": 6,
            },
            reasoning_effort={"pattern_discovery": "high"},
            stages={
                "pattern_discovery": ResearchStageConfig(
                    prompt_template="prompts/custom.txt",
                    max_output_tokens=2048,
                )
            },
        )

        settings = ResearchSettings.from_config(config)

        self.assertEqual(settings.maximum_candidate_evidence, 120)
        self.assertEqual(settings.maximum_macro_evidence, 32)
        self.assertEqual(settings.maximum_market_drivers, 6)
        self.assertEqual(settings.reasoning_effort["pattern_discovery"], "high")
        self.assertEqual(
            settings.prompt_templates["pattern_discovery"],
            "prompts/custom.txt",
        )
        self.assertEqual(settings.stage_max_output_tokens["pattern_discovery"], 2048)

    def test_settings_require_validated_contract(self):
        with self.assertRaises(TypeError):
            ResearchSettings.from_config({"enabled": True})  # type: ignore[arg-type]

    def test_canonical_contract_rejects_unsafe_policy(self):
        invalid_values = (
            {"schedule": "not-a-cron"},
            {"graph": {"depth": 5, "hard_depth": 4}},
            {"limits": {"maximum_candidate_evidence": 5}},
            {"lifecycle_thresholds": {"weakening_days": 50, "archive_days": 30}},
            {"stages": {"claim_extraction": {"max_output_tokens": 5000}}},
            {"model_overrides": {"unknown_stage": "model"}},
            {"reasoning_effort": {"pattern_discovery": "extreme"}},
        )

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ResearchIntelligenceConfig.model_validate(value)


if __name__ == "__main__":
    unittest.main()
