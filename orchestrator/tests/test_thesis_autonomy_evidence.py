"""Tests for thesis autonomy config bounds, evidence selection, and market close gating."""

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import yaml
from pydantic import ValidationError
from sqlalchemy import create_engine, text

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

# Import support first so environment is configured
from thesis_autonomy_support import (
    CANDIDATE,
    EXISTING_ID,
    NOW,
    MemorySession,
    ScriptedAuditor,
    ScriptedChallenger,
    ScriptedRunner,
    cycle_config,
    evidence_item,
    evidence_items,
    run_cycle,
)

from contracts.runtime_config import AppConfig, ThesisAutonomyConfig
from research_intelligence.contracts import EvidenceSignal, NormalizedEvidence
from research_intelligence.evidence import EvidenceCollection, EvidenceRegistry
from thesis_autonomy import (
    _attach_cited_evidence,
    _candidate_evidence,
    _candidate_expected_at,
    _candidate_source_gate,
    _close_at_or_before,
    _collect_evidence,
    _contradiction_signals,
    _signal_from_row,
    run_autonomous_thesis_cycle,
    thesis_autonomy_identity,
)
from thesis_fusion import evaluate_thesis
from thesis_scoring import assess_evidence


class ThesisAutonomyConfigTests(unittest.TestCase):
    def test_strict_defaults_match_the_checked_in_profile(self):
        config = ThesisAutonomyConfig()
        self.assertTrue(config.enabled)
        self.assertTrue(config.schedule_enabled)
        self.assertIsNone(config.schedule)
        self.assertEqual(config.lookback_days, 30)
        self.assertEqual(config.maximum_evidence, 96)
        self.assertEqual(config.maximum_promoted, 64)
        self.assertEqual(config.maximum_challenges_per_run, 25)
        self.assertEqual(config.event_debounce_minutes, 60)
        self.assertEqual(config.model_budget_usd_per_run, 0.75)
        self.assertIsNone(config.reasoning_effort)
        self.assertIsNone(config.model_override)
        self.assertIsNone(config.max_output_tokens)
        self.assertIsNone(config.cost)
        self.assertIsNone(config.liquidity)
        self.assertIsNone(config.downside)

    def test_unknown_fields_and_out_of_bounds_values_are_rejected(self):
        with self.assertRaises(ValidationError):
            ThesisAutonomyConfig.model_validate({"mystery_key": 1})
        for kwargs in (
            {"maximum_evidence": 2001},
            {"maximum_evidence": 0},
            {"maximum_promoted": 65},
            {"maximum_promoted": 0},
            {"maximum_challenges_per_run": 0},
            {"event_debounce_minutes": 0},
            {"event_debounce_minutes": 1441},
            {"model_budget_usd_per_run": -0.01},
            {"liquidity": 1.5},
            {"downside": -0.1},
            {"cost": 101.0},
            {"max_output_tokens": 0},
            {"max_output_tokens": 100001},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValidationError):
                    ThesisAutonomyConfig(**kwargs)

    def test_checked_in_profile_sets_a_high_output_token_ceiling(self):

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        raw = yaml.safe_load(config_path.read_text())
        section = raw["thesis_autonomy"]
        self.assertTrue(section["enabled"])
        self.assertEqual(section["maximum_evidence"], 400)
        self.assertEqual(section["maximum_promoted"], 4)
        self.assertEqual(section["maximum_challenges_per_run"], 4)
        self.assertEqual(section["maximum_event_runs_per_day"], 2)
        self.assertEqual(section["max_output_tokens"], 16384)
        self.assertEqual(section["model_budget_usd_per_run"], 0.75)
        self.assertEqual(section["event_debounce_minutes"], 180)
        self.assertEqual(
            section["model_override"], "nvidia/nemotron-3-super-120b-a12b:free"
        )
        # The desk llm policy is explicit: no silent fallback.
        llm = raw["llm"]
        self.assertEqual(llm["max_output_tokens"]["thesis_autonomy"], 16384)
        self.assertTrue(llm["structured_response"]["thesis_autonomy"])
        self.assertFalse(llm["require_parameters"]["thesis_autonomy"])
        self.assertEqual(llm["max_prices"]["thesis_autonomy"]["completion"], 3.5)

    def test_per_run_budget_never_exceeds_the_daily_cap(self):
        with self.assertRaisesRegex(ValidationError, "daily budget"):
            AppConfig(
                database={
                    "host": "localhost",
                    "name": "test",
                    "user": "u",
                    "password": "p",
                },
                budgets={"daily_llm_usd": 0.5},
                thesis_autonomy={"model_budget_usd_per_run": 1.0},
            )
        # Equal to the daily cap is valid.
        AppConfig(
            database={
                "host": "localhost",
                "name": "test",
                "user": "u",
                "password": "p",
            },
            budgets={"daily_llm_usd": 0.5},
            thesis_autonomy={"model_budget_usd_per_run": 0.5},
        )

    def test_identity_subset_is_bounded_and_stable(self):
        identity = thesis_autonomy_identity(cycle_config())
        self.assertEqual(
            set(identity),
            {
                "lookback_days",
                "maximum_evidence",
                "maximum_promoted",
                "maximum_challenges_per_run",
                "event_debounce_minutes",
                "maximum_event_runs_per_day",
                "falsification_budget_fraction",
                "minimum_supporting_source_families",
                "require_cited_excerpts",
                "require_opposing_variants",
                "model_budget_usd_per_run",
                "reasoning_effort",
                "model_override",
                "max_output_tokens",
                "cost",
                "liquidity",
                "downside",
            },
        )
        self.assertEqual(identity["maximum_evidence"], 96)
        self.assertEqual(
            thesis_autonomy_identity(cycle_config(maximum_evidence=128))[
                "maximum_evidence"
            ],
            128,
        )
        # Missing sections resolve to frozen defaults, never to junk.
        self.assertEqual(thesis_autonomy_identity({})["maximum_promoted"], 64)


class EvidenceSelectionTests(unittest.TestCase):
    def test_selection_is_safe_deterministic_and_bounded(self):
        items = [
            evidence_item(index, point_in_time_safe=index % 3 != 0)
            for index in range(20)
        ]
        collection = EvidenceCollection(
            items=tuple(items), failures={"macro_observations": "boom"}
        )
        with patch.object(
            EvidenceRegistry, "collect", return_value=collection
        ) as collect:
            selected, failures = _collect_evidence(
                MagicMock(),
                {"lookback_days": 30, "maximum_evidence": 10},
                reference=NOW,
            )
        self.assertEqual(len(selected), 10)
        self.assertTrue(all(item.point_in_time_safe for item in selected))
        stamps = [item.source_timestamp for item in selected]
        self.assertEqual(stamps, sorted(stamps))
        refs = [item.ref for item in selected]
        self.assertEqual(refs, sorted(refs))
        self.assertEqual(failures, {"macro_observations": "boom"})
        collect.assert_called_once()
        call_kwargs = collect.call_args.kwargs
        self.assertEqual(call_kwargs["rolling_window_days"], 30)
        self.assertEqual(call_kwargs["limit"], 10)
        self.assertEqual(call_kwargs["now"], NOW)
        # The cycle reference is enforced as a replay cutoff so adapters
        # bound source timestamps by `until` and the context's filter
        # enforces the source/availability cutoffs.
        context = call_kwargs["context"]
        self.assertTrue(context.is_replay)
        self.assertEqual(context.as_of, NOW)

    def test_replay_context_bounds_source_and_availability(self):
        items = (
            evidence_item(
                1,
                source_timestamp=NOW - timedelta(days=1),
                available_at=NOW - timedelta(days=1),
            ),
            evidence_item(
                2,
                source_timestamp=NOW + timedelta(days=1),
                available_at=NOW + timedelta(days=1),
            ),
            evidence_item(
                3,
                source_timestamp=NOW - timedelta(days=2),
                available_at=NOW + timedelta(hours=1),
            ),
        )
        captured: dict[str, Any] = {}

        def replay_bounded_collect(session, **kwargs):
            captured.update(kwargs)
            return EvidenceCollection(
                items=tuple(kwargs["context"].filter_evidence(items)), failures={}
            )

        with patch.object(
            EvidenceRegistry, "collect", side_effect=replay_bounded_collect
        ):
            selected, failures = _collect_evidence(
                MagicMock(),
                {"lookback_days": 30, "maximum_evidence": 10},
                reference=NOW,
            )
        self.assertEqual(failures, {})
        # Only the item whose source AND availability precede the reference
        # survives; a future source or a future availability alone excludes.
        self.assertEqual([item.ref for item in selected], [items[0].ref])
        context = captured["context"]
        self.assertEqual(context.as_of, NOW)
        self.assertEqual(context.audit.future_evidence_excluded, 2)

    def test_lookback_and_limit_are_forwarded(self):
        collection = EvidenceCollection(items=(), failures={})
        with patch.object(
            EvidenceRegistry, "collect", return_value=collection
        ) as collect:
            _collect_evidence(
                MagicMock(),
                {"lookback_days": 45, "maximum_evidence": 96},
                reference=NOW,
            )
        self.assertEqual(collect.call_args.kwargs["rolling_window_days"], 45)
        self.assertEqual(collect.call_args.kwargs["limit"], 96)


class MarketCloseTests(unittest.TestCase):
    def test_close_rejects_price_older_than_freshness_window(self):
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(days=30), 100.0)]

        self.assertIsNone(_close_at_or_before(session, "ACME", NOW))

        session.market_data["ACME"].append((NOW - timedelta(days=2), 110.0))
        self.assertEqual(_close_at_or_before(session, "ACME", NOW), 110.0)

    def test_latest_close_uses_market_data_composite_key(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """CREATE TABLE market_data (
                           symbol TEXT NOT NULL,
                           timeframe TEXT NOT NULL,
                           timestamp TIMESTAMP NOT NULL,
                           close REAL,
                           source TEXT NOT NULL,
                           created_at TIMESTAMP NOT NULL,
                           updated_at TIMESTAMP,
                           PRIMARY KEY (symbol, timeframe, timestamp)
                       )"""
                )
            )
            connection.execute(
                text(
                    """INSERT INTO market_data
                           (symbol, timeframe, timestamp, close, source, created_at)
                       VALUES
                           ('ACME', '1d', :older, 101.0, 'daily', :older),
                           ('ACME', '1d', :latest, 102.0, 'daily', :latest),
                           ('ACME', 'PRICE', :latest, 103.0, 'live', :latest)"""
                ),
                {"older": NOW - timedelta(minutes=1), "latest": NOW},
            )

            close = _close_at_or_before(connection, "ACME", NOW)

        self.assertEqual(close, 103.0)

    def test_lookup_canonicalizes_symbols_and_honors_availability_cutoff(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """CREATE TABLE market_data (
                           symbol TEXT NOT NULL,
                           timeframe TEXT NOT NULL,
                           timestamp TIMESTAMP NOT NULL,
                           close REAL,
                           source TEXT NOT NULL,
                           created_at TIMESTAMP NOT NULL,
                           updated_at TIMESTAMP,
                           PRIMARY KEY (symbol, timeframe, timestamp)
                       )"""
                )
            )
            connection.execute(
                text(
                    """INSERT INTO market_data
                           (symbol, timeframe, timestamp, close, source, created_at)
                       VALUES
                           ('ACME', '1d', :older, 101.0, 'daily', :older),
                           ('ACME', '1d', :newer, 103.0, 'daily', :now)"""
                ),
                {
                    "older": NOW - timedelta(days=2),
                    "newer": NOW - timedelta(days=1),
                    "now": NOW,
                },
            )
            # Mixed/lowercase/whitespace persisted identities still match
            # the uppercase market rows (dots/suffixes are preserved).
            self.assertEqual(_close_at_or_before(connection, " acme ", NOW), 103.0)
            self.assertEqual(_close_at_or_before(connection, "Acme", NOW), 103.0)
            # A bar persisted after the run/replay cutoff is invisible even
            # though it is timestamped before as_of.
            self.assertEqual(
                _close_at_or_before(
                    connection, "ACME", NOW, available_at=NOW - timedelta(days=1)
                ),
                101.0,
            )

    def test_blank_symbol_and_unavailable_bars_return_none(self):
        self.assertIsNone(_close_at_or_before(None, "   ", NOW))
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """CREATE TABLE market_data (
                           symbol TEXT NOT NULL,
                           timeframe TEXT NOT NULL,
                           timestamp TIMESTAMP NOT NULL,
                           close REAL,
                           source TEXT NOT NULL,
                           created_at TIMESTAMP NOT NULL,
                           updated_at TIMESTAMP,
                           PRIMARY KEY (symbol, timeframe, timestamp)
                       )"""
                )
            )
            connection.execute(
                text(
                    """INSERT INTO market_data
                           (symbol, timeframe, timestamp, close, source, created_at)
                       VALUES ('ACME', '1d', :ts, 101.0, 'daily', :created)"""
                ),
                {
                    "ts": NOW - timedelta(days=2),
                    "created": NOW - timedelta(days=2),
                },
            )
            # The bar is timestamped at/before as_of but was not persisted
            # until after the run/replay cutoff.
            self.assertIsNone(
                _close_at_or_before(
                    connection,
                    "ACME",
                    NOW,
                    available_at=NOW - timedelta(days=3),
                )
            )
            self.assertIsNone(
                _close_at_or_before(connection, "ACME", NOW - timedelta(days=4))
            )

    def test_row_revised_after_cutoff_is_excluded_unchanged_row_stays_eligible(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """CREATE TABLE market_data (
                           symbol TEXT NOT NULL,
                           timeframe TEXT NOT NULL,
                           timestamp TIMESTAMP NOT NULL,
                           close REAL,
                           source TEXT NOT NULL,
                           created_at TIMESTAMP NOT NULL,
                           updated_at TIMESTAMP,
                           PRIMARY KEY (symbol, timeframe, timestamp)
                       )"""
                )
            )
            connection.execute(
                text(
                    """INSERT INTO market_data
                           (symbol, timeframe, timestamp, close, source,
                            created_at, updated_at)
                       VALUES
                           -- unchanged control: created and last revised
                           -- before the cutoff, stays eligible.
                           ('ACME', '1d', :control_ts, 101.0, 'daily',
                            :before, :before),
                           -- revised after the cutoff: created before the
                           -- cutoff but its row was mutated after it, so a
                           -- replay at the cutoff must not see it.
                           ('ACME', '1d', :revised_ts, 102.0, 'daily',
                            :before, :after)"""
                ),
                {
                    "control_ts": NOW - timedelta(days=3),
                    "revised_ts": NOW - timedelta(days=2),
                    "before": NOW - timedelta(days=3),
                    "after": NOW - timedelta(days=1),
                },
            )
            cutoff = NOW - timedelta(days=2)
            # The later bar was revised after the cutoff, so the close at
            # the cutoff falls back to the unchanged pre-cutoff bar.
            self.assertEqual(
                _close_at_or_before(connection, "ACME", NOW, available_at=cutoff),
                101.0,
            )
            # Unchanged rows stay eligible at every earlier cutoff.
            self.assertEqual(
                _close_at_or_before(
                    connection,
                    "ACME",
                    NOW,
                    available_at=cutoff - timedelta(days=1),
                ),
                101.0,
            )

    def test_null_updated_at_falls_back_to_created_at(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """CREATE TABLE market_data (
                           symbol TEXT NOT NULL,
                           timeframe TEXT NOT NULL,
                           timestamp TIMESTAMP NOT NULL,
                           close REAL,
                           source TEXT NOT NULL,
                           created_at TIMESTAMP NOT NULL,
                           updated_at TIMESTAMP,
                           PRIMARY KEY (symbol, timeframe, timestamp)
                       )"""
                )
            )
            # Legacy rows carry no updated_at: COALESCE(updated_at,
            # created_at) must treat them by their ingestion time.
            connection.execute(
                text(
                    """INSERT INTO market_data
                           (symbol, timeframe, timestamp, close, source, created_at)
                       VALUES ('ACME', '1d', :ts, 101.0, 'daily', :created)"""
                ),
                {
                    "ts": NOW - timedelta(days=2),
                    "created": NOW - timedelta(days=2),
                },
            )
            self.assertEqual(
                _close_at_or_before(
                    connection,
                    "ACME",
                    NOW,
                    available_at=NOW - timedelta(days=2),
                ),
                101.0,
            )
            self.assertIsNone(
                _close_at_or_before(
                    connection,
                    "ACME",
                    NOW,
                    available_at=NOW - timedelta(days=3),
                )
            )


class LateEvidenceAttachmentTests(unittest.TestCase):
    def _signal(self, fingerprint: str = "e" * 64) -> EvidenceSignal:
        return EvidenceSignal.create(
            evidence_id="claim:late",
            evidence_type="source_claim",
            relationship="supports",
            source_name="filings",
            source_family="filings",
            origin_key="sec:10q:acme:2026q2",
            independence_key="filings:acme",
            evidence_fingerprint=fingerprint,
            source_timestamp=NOW - timedelta(days=2),
            available_at=NOW - timedelta(days=2),
            # Explicit positive scores plus a verbatim excerpt make the
            # signal auditable under ``is_auditable_evidence``.
            quality_score=0.9,
            entailment_score=0.9,
            freshness_score=0.8,
            provenance={
                "excerpt": "The disclosed change raises the current-quarter outlook.",
            },
        )

    def test_persisted_link_attached_after_cutoff_is_excluded(self):
        # An old source whose link row was persisted after the cutoff is
        # invisible to a historical evaluation (the filtering fake applies
        # the same created_at/source/availability predicates as production),
        # so the old source cannot leak into an older accepted score.
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:late",
            evidence_fingerprint="e" * 64,
            source_timestamp=NOW - timedelta(days=2),
            available_at=NOW - timedelta(days=2),
            created_at=NOW,  # attached after the cutoff
        )
        result = evaluate_thesis(
            session, str(EXISTING_ID), as_of=NOW - timedelta(days=1)
        )
        self.assertEqual(result["evidence"]["support_count"], 0)
        self.assertIsNone(result["evidence"]["confidence"])

    def test_explicit_same_cycle_evidence_scores_at_the_cutoff(self):
        # The same evidence enters scoring explicitly as the current-cycle
        # artifact (the invocation's own derived signal), so a fresh cycle
        # still scores its just-attached evidence at its own reference.
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:late",
            evidence_fingerprint="e" * 64,
            source_timestamp=NOW - timedelta(days=2),
            available_at=NOW - timedelta(days=2),
            created_at=NOW,  # attached after the cutoff
        )
        cutoff = NOW - timedelta(days=1)
        result = evaluate_thesis(
            session,
            str(EXISTING_ID),
            as_of=cutoff,
            current_evidence=(self._signal(),),
        )
        self.assertEqual(result["evidence"]["support_count"], 1)
        # The persisted row and the explicit signal share a fingerprint, so
        # the explicit input dedupes to exactly one scored evidence item.
        self.assertEqual(result["evidence"]["unique_evidence_count"], 1)
        self.assertGreater(result["evidence"]["support_mass"], 0.0)

    def test_cutoff_valid_persisted_link_and_explicit_signal_do_not_double_count(
        self,
    ):
        # A persisted link that predates the cutoff and an explicit
        # current-cycle signal with the same fingerprint are the same
        # evidence and score exactly once.
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:seed",
            evidence_fingerprint="c" * 64,
            excerpt="The disclosed change raises the current-quarter outlook.",
            quality_score=0.9,
            entailment_score=0.9,
            freshness_score=0.8,
        )
        result = evaluate_thesis(
            session,
            str(EXISTING_ID),
            as_of=NOW - timedelta(days=1),
            current_evidence=(self._signal(fingerprint="c" * 64),),
        )
        self.assertEqual(result["evidence"]["support_count"], 1)
        self.assertEqual(result["evidence"]["unique_evidence_count"], 1)


class AuditableEvidenceTests(unittest.TestCase):
    """The auditable-evidence predicate gates promotion, contradiction
    attachment, and scoring identically across the same-cycle and persisted
    paths."""

    def _run_with_evidence(self, items, **overrides):
        session = MemorySession()
        kwargs = {
            "as_of": NOW,
            "runner": ScriptedRunner(CANDIDATE),
            "challenger": ScriptedChallenger(),
            "auditor": ScriptedAuditor(),
        }
        kwargs.update(overrides)
        with patch.object(
            EvidenceRegistry,
            "collect",
            return_value=EvidenceCollection(items=tuple(items), failures={}),
        ):
            result = run_autonomous_thesis_cycle(session, cycle_config(), **kwargs)
        return session, result

    def test_one_valid_positive_quality_entailed_report_satisfies_the_gate(self):
        # One auditable cited item (nonblank excerpt, positive quality,
        # entailed) satisfies the support gate even when the other cited
        # items are excerpt-less placeholders.
        items = [evidence_item(0)] + [
            evidence_item(index, bounded_excerpt=None) for index in range(1, 6)
        ]
        session, result = self._run_with_evidence(items)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["promoted_count"], 1)
        self.assertEqual(result["source_gate_rejections"], 0)
        self.assertEqual(len(session.evidence), 3)

    def test_all_unusable_support_rejects_promotion(self):
        # Every cited item is excerpt-less: no cited support is auditable,
        # so every candidate fails the source gate and nothing promotes.
        items = [evidence_item(index, bounded_excerpt=None) for index in range(6)]
        session, result = self._run_with_evidence(items)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["promoted_count"], 0)
        self.assertGreater(result["source_gate_rejections"], 0)
        self.assertGreater(result["promotion_gate_rejections"], 0)

    def test_source_gate_requires_positive_entailment(self):
        catalog = {item.ref: item for item in evidence_items(1)}
        candidate = SimpleNamespace(evidence_refs=("source_claim:claim:0000",))
        unentailed = _candidate_source_gate(
            candidate,
            catalog,
            minimum_families=1,
            require_excerpts=False,
            entailment_score=0.0,
        )
        self.assertEqual(unentailed, "no auditable supporting evidence")
        self.assertIsNone(
            _candidate_source_gate(
                candidate,
                catalog,
                minimum_families=1,
                require_excerpts=False,
                entailment_score=1.0,
            )
        )

    def test_duplicate_cited_support_excerpts_do_not_double_count(self):
        # The same cited ref listed twice is one fingerprint: same-cycle
        # signals and persisted attachment both dedupe it exactly once.
        catalog = {item.ref: item for item in evidence_items(1)}
        candidate = SimpleNamespace(
            evidence_refs=(
                "source_claim:claim:0000",
                "source_claim:claim:0000",
            )
        )
        signals = _candidate_evidence(candidate, catalog, entailment_score=1.0)
        self.assertEqual(len(signals), 1)
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        outcome = _attach_cited_evidence(
            session,
            EXISTING_ID,
            candidate,
            catalog,
            entailment_score=1.0,
        )
        self.assertEqual(outcome["attached"], 1)
        self.assertEqual(len(session.evidence), 1)

    def test_null_excerpt_contradiction_placeholders_are_never_attached(self):
        # A challenger citation that resolves to an excerpt-less collected
        # item is not auditable, so it attaches nothing as a contradiction
        # and contributes no contradiction mass.
        items = [
            evidence_item(0),
            evidence_item(1),
            evidence_item(2),
            evidence_item(3, bounded_excerpt=None),  # placeholder
        ]
        session, result = self._run_with_evidence(
            items,
            challenger=ScriptedChallenger(
                proposal={
                    "kind": "counter_evidence",
                    "statement": "The cited evidence supports the opposite view",
                    "citations": ["source_claim:claim:0003"],
                }
            ),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["promoted_count"], 1)
        self.assertEqual(result["contradictions_attached"], 0)
        self.assertEqual(
            [row for row in session.evidence if row["relationship"] == "contradicts"],
            [],
        )

    def test_top_audited_shape_placeholders_contribute_no_mass(self):
        # The top audited shape: one auditable support plus two persisted
        # null-excerpt/zero-quality contradiction placeholders. The
        # placeholders remain historical/context rows; they never attach as
        # contradictions, never damp confidence, and never make the shape
        # directionally scored on their own.
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:audited-support",
            evidence_fingerprint="a" * 64,
            excerpt="Disclosed cost trend confirms margin expansion.",
            quality_score=0.9,
            entailment_score=0.9,
            freshness_score=0.8,
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:placeholder-a",
            evidence_fingerprint="b" * 64,
            excerpt=None,
            quality_score=0.0,
            entailment_score=0.0,
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:placeholder-b",
            evidence_fingerprint="c" * 64,
            excerpt=None,
            quality_score=0.0,
            entailment_score=0.0,
        )
        proposal = {
            "kind": "counter_evidence",
            "statement": "The cited evidence supports the opposite view",
            "citations": [
                "source_claim:claim:placeholder-a",
                "source_claim:claim:placeholder-b",
            ],
        }

        class SecondPassOnlyChallenger(ScriptedChallenger):
            """Oppose only the second pass so the first pass promotes
            normally (a first-pass proposal must cite collected catalog
            evidence, which these placeholder ids are not)."""

            def __init__(self, proposal):
                super().__init__(proposal=proposal)
                self._calls = 0

            def challenge(self, snapshot, evidence):
                self._calls += 1
                if self._calls == 1:
                    return None
                return super().challenge(snapshot, evidence)

        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=SecondPassOnlyChallenger(proposal),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["challenger_failures"], 0)
        self.assertEqual(result["promoted_count"], 1)
        # The second pass challenges the seeded thesis and attaches nothing
        # for the placeholder citations.
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["contradictions_attached"], 0)
        self.assertEqual(
            [row for row in session.evidence if row["relationship"] == "contradicts"],
            [],
        )
        # Persisted-path scoring: the rebuilt signals show the support
        # contributing and both placeholders contributing nothing.
        rows = [row for row in session.evidence if row["thesis_id"] == EXISTING_ID]
        score = assess_evidence(tuple(_signal_from_row(row) for row in rows))
        self.assertEqual(score.support_count, 1)
        self.assertGreater(score.support_mass, 0.0)
        self.assertEqual(score.contradiction_count, 0)
        self.assertEqual(score.contradiction_mass, 0.0)
        self.assertEqual(score.context_count, 2)
        placeholder_rows = [
            row for row in rows if row["evidence_id"] != "claim:audited-support"
        ]
        alone = assess_evidence(
            tuple(_signal_from_row(row) for row in placeholder_rows)
        )
        self.assertIsNone(alone.confidence)
        self.assertEqual(alone.contradiction_mass, 0.0)

    def test_contradiction_signals_drop_zero_quality_placeholders(self):
        # A decision citing persisted placeholder rows (null excerpt, zero
        # quality) yields no contradiction signals for attachment or the
        # same-cycle recompute.
        rows = [
            {
                "evidence_type": "story_cluster",
                "evidence_id": "story:fred-a",
                "relationship": "context",
                "excerpt": None,
                "source_family": "fred",
                "origin_key": None,
                "independence_key": None,
                "evidence_fingerprint": "b" * 64,
                "source_timestamp": NOW - timedelta(days=2),
                "available_at": NOW - timedelta(days=2),
                "quality_score": 0.0,
                "entailment_score": 0.0,
                "freshness_score": 0.0,
                "effective_weight": 1.0,
                "created_at": NOW - timedelta(days=2),
            },
            {
                "evidence_type": "story_cluster",
                "evidence_id": "story:fred-b",
                "relationship": "context",
                "excerpt": None,
                "source_family": "fred",
                "origin_key": None,
                "independence_key": None,
                "evidence_fingerprint": "c" * 64,
                "source_timestamp": NOW - timedelta(days=2),
                "available_at": NOW - timedelta(days=2),
                "quality_score": 0.0,
                "entailment_score": 0.0,
                "freshness_score": 0.0,
                "effective_weight": 1.0,
                "created_at": NOW - timedelta(days=2),
            },
        ]
        signals = tuple(_signal_from_row(row) for row in rows)
        decision = SimpleNamespace(
            runner_findings=[
                SimpleNamespace(
                    citations=(
                        "story_cluster:story:fred-a",
                        "story_cluster:story:fred-b",
                    )
                )
            ]
        )
        self.assertEqual(_contradiction_signals(decision, signals), ())

    def test_structured_observation_contradiction_is_picked_but_empty_payload_is_not(
        self,
    ):
        # A contradiction with a real structured observation payload and
        # positive quality is auditable without an excerpt; an identical
        # row whose structured payload is empty (or whose quality is zero)
        # stays a placeholder and is never picked.
        def signal_for(evidence_id, fingerprint, quality, structured):
            return EvidenceSignal.create(
                evidence_id=evidence_id,
                evidence_type="macro_observation",
                relationship="context",
                source_name="fred",
                source_family="fred",
                origin_key=f"fred:{evidence_id}",
                independence_key="fred:series",
                evidence_fingerprint=fingerprint,
                source_timestamp=NOW - timedelta(days=1),
                available_at=NOW - timedelta(days=1),
                quality_score=quality,
                entailment_score=0.9,
                freshness_score=0.8,
                provenance={"structured_fields": structured},
            )

        structured = signal_for(
            "macro:fred-x", "d" * 64, 0.9, {"series_id": "FRED_X", "value": 42.0}
        )
        empty_payload = signal_for("macro:fred-empty", "e" * 64, 0.9, {})
        zero_quality = signal_for("macro:fred-zero", "f" * 64, 0.0, {"series_id": "Y"})
        decision = SimpleNamespace(
            runner_findings=[
                SimpleNamespace(
                    citations=(
                        "macro_observation:macro:fred-x",
                        "macro_observation:macro:fred-empty",
                        "macro_observation:macro:fred-zero",
                    )
                )
            ]
        )
        picked = _contradiction_signals(
            decision, (structured, empty_payload, zero_quality)
        )
        self.assertEqual([signal.evidence_id for signal in picked], ["macro:fred-x"])

    def test_same_cycle_and_persisted_scoring_parity(self):
        # The same cited evidence scores identically whether it enters
        # assess_evidence as the cycle's explicit current-cycle signals or
        # as rows rebuilt from the persisted link table.
        catalog = {item.ref: item for item in evidence_items(2)}
        candidate = SimpleNamespace(
            evidence_refs=(
                "source_claim:claim:0000",
                "source_claim:claim:0001",
            )
        )
        entailment_score = 1.0
        same_cycle = assess_evidence(
            _candidate_evidence(candidate, catalog, entailment_score=entailment_score)
        )
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        _attach_cited_evidence(
            session,
            EXISTING_ID,
            candidate,
            catalog,
            entailment_score=entailment_score,
        )
        persisted_rows = [
            row for row in session.evidence if row["thesis_id"] == EXISTING_ID
        ]
        persisted = assess_evidence(
            tuple(_signal_from_row(row) for row in persisted_rows)
        )
        self.assertEqual(persisted.support_count, same_cycle.support_count)
        self.assertEqual(
            persisted.support_evidence_ids, same_cycle.support_evidence_ids
        )
        self.assertAlmostEqual(persisted.support_mass, same_cycle.support_mass)
        self.assertAlmostEqual(persisted.confidence, same_cycle.confidence)
        self.assertEqual(persisted.contradiction_mass, same_cycle.contradiction_mass)

    def test_persisted_evaluate_path_scores_auditable_evidence(self):
        # evaluate_thesis' persisted path rebuilds the excerpt into the
        # signal, so a stored auditable row scores as support while a
        # null-excerpt zero-quality row contributes nothing.
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:audited-support",
            evidence_fingerprint="a" * 64,
            excerpt="Disclosed cost trend confirms margin expansion.",
            quality_score=0.9,
            entailment_score=0.9,
            freshness_score=0.8,
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:placeholder",
            evidence_fingerprint="b" * 64,
            excerpt=None,
            quality_score=0.0,
            entailment_score=0.0,
        )
        result = evaluate_thesis(session, str(EXISTING_ID), as_of=NOW)
        self.assertEqual(result["evidence"]["support_count"], 1)
        self.assertGreater(result["evidence"]["support_mass"], 0.0)
        self.assertEqual(result["evidence"]["contradiction_count"], 0)
        self.assertEqual(result["evidence"]["context_count"], 1)
        self.assertIsNotNone(result["evidence"]["confidence"])


class CandidateExpectedAtTests(unittest.TestCase):
    def test_uses_earliest_future_cited_announced_earnings_date(self):
        evidence = NormalizedEvidence.create(
            evidence_type="official_document",
            evidence_id="expectations:aapl:2026-08-15",
            source_name="Apple",
            source_timestamp=NOW,
            available_at=NOW,
            title="Consensus and announced earnings date",
            point_in_time_safe=True,
            provenance={
                "source": "company_expectations",
                "metadata": {"next_earnings": {"reportDate": "2026-08-20"}},
            },
        )
        candidate = SimpleNamespace(evidence_refs=(evidence.ref,))
        self.assertEqual(
            _candidate_expected_at(candidate, {evidence.ref: evidence}, reference=NOW),
            datetime(2026, 8, 20, tzinfo=UTC),
        )


if __name__ == '__main__':
    unittest.main()
