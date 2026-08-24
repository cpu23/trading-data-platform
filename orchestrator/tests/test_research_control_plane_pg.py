"""Real PostgreSQL invariants for autonomous research planning and recovery."""

import threading
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from pg_support import parse_config, provision, require_postgres, truncate
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from analysis_jobs import reconcile_jobs
from research_control_plane.domain import PriorityInputs, QuestionCandidate
from research_control_plane.repository import (
    QuestionDraft,
    enqueue_planner_job,
    propagate_event_dependencies,
    reconcile_terminal_work_order_failures,
    run_planner,
    upsert_question,
)
from research_control_plane.skills import ensure_skill_versions, execute_work_order

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


class ResearchControlPlanePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = require_postgres()
        cls.config = parse_config(cls.url)
        cls.config.update(
            {
                "budgets": {"daily_llm_usd": 0.15, "warn_at_pct": 80},
                "research_control_plane": {
                    "enabled": True,
                    "maximum_questions_per_plan": 20,
                    "maximum_work_orders_per_plan": 1,
                    "maximum_runtime_seconds_per_plan": 900,
                    "model_budget_usd_per_plan": 0.15,
                    "minimum_priority": 0,
                    "catalyst_lookahead_days": 30,
                    "stale_question_days": 14,
                    "priority_policy_version": "v1",
                    "materiality_policy_version": "v1",
                },
            }
        )
        provision(cls.config, cls.url)
        from db import get_engine, get_session

        cls.engine = get_engine(cls.config)
        cls.get_session = staticmethod(get_session)

    def setUp(self):
        truncate(
            self.config,
            (
                "research_questions",
                "research_dependency_nodes",
                "research_skill_versions",
                "budget_reservations",
                "processing_log",
                "analysis_jobs",
                "cycle_runs",
                "ui_events",
                "investment_themes",
                "market_data",
            ),
        )
        with self.get_session(self.config) as session:
            ensure_skill_versions(session)

    @staticmethod
    def _draft(
        target_ref: str, *, cost: str = "0.10", cutoff: datetime = NOW
    ) -> QuestionDraft:
        return QuestionDraft(
            candidate=QuestionCandidate(
                origin_kind="source_event",
                question_type="evidence_refresh",
                atomic_question=f"What changed for {target_ref}?",
                target_kind="entity",
                target_ref=target_ref,
                accepted_cutoff=cutoff,
                required_evidence_shape={"answer": "cited change"},
                acceptable_source_families=("issuer_filing",),
            ),
            priority=PriorityInputs(
                materiality=Decimal("0.8"),
                uncertainty=Decimal("0.7"),
                discrimination_power=Decimal("0.9"),
                urgency=Decimal("0.8"),
                freshness_gap=Decimal("1"),
                resolvability=Decimal("0.9"),
                expected_cost_usd=Decimal(cost),
                expected_runtime_seconds=30,
            ),
            not_before=cutoff,
            due_at=cutoff + timedelta(days=1),
            expires_at=cutoff + timedelta(days=2),
        )

    def _question(self, target_ref: str, *, cost: str = "0.10"):
        with self.get_session(self.config) as session:
            question = upsert_question(session, self._draft(target_ref, cost=cost))
        self.assertIsNotNone(question)
        return question["id"]

    def _run(self, correlation_id=None):
        with self.get_session(self.config) as session:
            return run_planner(
                session,
                self.config,
                correlation_id=correlation_id or uuid4(),
                trigger_kind="manual",
                trigger_ref="postgres-test",
                accepted_cutoff=NOW,
            )

    def test_plan_reserves_global_budget_and_links_exact_skill_and_job(self):
        question_id = self._question("NVDA")
        result = self._run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.selected_count, 1)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                    SELECT w.question_id, w.budget_reservation_id,
                           w.analysis_job_id, w.skill_version_id,
                           w.status AS work_status, q.status AS question_status,
                           b.status AS budget_status, b.estimated_usd,
                           j.state AS job_state
                    FROM research_work_orders w
                    JOIN research_questions q ON q.id = w.question_id
                    JOIN budget_reservations b ON b.id = w.budget_reservation_id
                    JOIN analysis_jobs j ON j.id = w.analysis_job_id
                    """
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(row["question_id"], question_id)
        self.assertEqual(row["work_status"], "queued")
        self.assertEqual(row["question_status"], "queued")
        self.assertEqual(row["budget_status"], "active")
        self.assertEqual(float(row["estimated_usd"]), 0.1)
        self.assertEqual(row["job_state"], "queued")

    def test_event_propagation_targets_graph_and_rejects_stale_rewind(self):
        theme_id = uuid4()
        affected_id = uuid4()
        unaffected_id = uuid4()
        created_at = NOW - timedelta(days=10)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO investment_themes (
                        id, name, definition, created_at, updated_at
                    ) VALUES (
                        :id, 'graph-fixture', 'Dependency graph fixture',
                        :created_at, :created_at
                    )
                    """
                ),
                {"id": theme_id, "created_at": created_at},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO investment_theses (
                        id, theme_id, company, symbol, claim, status,
                        created_at, updated_at
                    ) VALUES
                        (
                            :affected_id, :theme_id, 'Affected Inc', 'AAA',
                            'Affected thesis', 'active',
                            :created_at, :created_at
                        ),
                        (
                            :unaffected_id, :theme_id, 'Unaffected Inc', 'BBB',
                            'Unaffected thesis', 'active',
                            :created_at, :created_at
                        )
                    """
                ),
                {
                    "affected_id": affected_id,
                    "unaffected_id": unaffected_id,
                    "theme_id": theme_id,
                    "created_at": created_at,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO investment_thesis_versions (
                        thesis_id, version, claim, created_at
                    ) VALUES (:thesis_id, 1, 'Affected thesis', :created_at)
                    """
                ),
                {"thesis_id": affected_id, "created_at": created_at},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO investment_thesis_scenarios (
                        thesis_id, name, description, probability,
                        expected_return, created_at
                    ) VALUES (
                        :thesis_id, 'base', 'Demand remains durable',
                        0.6, 0.2, :created_at
                    )
                    """
                ),
                {"thesis_id": affected_id, "created_at": created_at},
            )
        event = {
            "event_id": str(uuid4()),
            "source": "fixture",
            "event_type": "filing_ingested",
            "entities": [{"symbol": "AAA"}],
            "markets": [],
        }
        with self.get_session(self.config) as session:
            result = propagate_event_dependencies(session, event, accepted_cutoff=NOW)
        self.assertEqual(result["theses_affected"], 1)
        self.assertGreaterEqual(result["nodes_touched"], 6)
        with self.engine.connect() as connection:
            thesis_nodes = (
                connection.execute(
                    text(
                        """
                    SELECT node_key, accepted_cutoff, dirty_since
                    FROM research_dependency_nodes
                    WHERE node_type = 'thesis'
                    ORDER BY node_key
                    """
                    )
                )
                .mappings()
                .all()
            )
            edge_kinds = set(
                connection.execute(
                    text("SELECT edge_kind FROM research_dependency_edges WHERE active")
                ).scalars()
            )
        self.assertEqual([row["node_key"] for row in thesis_nodes], [str(affected_id)])
        self.assertEqual(thesis_nodes[0]["accepted_cutoff"], NOW)
        self.assertEqual(thesis_nodes[0]["dirty_since"], NOW)
        self.assertTrue(
            {"affects", "derived_from", "mentions", "supports"}.issubset(edge_kinds)
        )

        stale_event = {**event, "event_id": str(uuid4())}
        with self.get_session(self.config) as session:
            propagate_event_dependencies(
                session,
                stale_event,
                accepted_cutoff=NOW - timedelta(days=1),
            )
        with self.engine.connect() as connection:
            preserved = (
                connection.execute(
                    text(
                        """
                    SELECT accepted_cutoff, dirty_since
                    FROM research_dependency_nodes
                    WHERE node_type = 'thesis' AND node_key = :node_key
                    """
                    ),
                    {"node_key": str(affected_id)},
                )
                .mappings()
                .one()
            )
        self.assertEqual(preserved["accepted_cutoff"], NOW)
        self.assertEqual(preserved["dirty_since"], NOW)

    def test_forecast_skill_resolves_cutoff_safely_and_records_feedback(self):
        theme_id = uuid4()
        thesis_id = uuid4()
        forecast_id = uuid4()
        created_at = NOW - timedelta(days=30)
        target_date = (NOW - timedelta(days=3)).date()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO investment_themes (id, name, definition)
                    VALUES (:id, :name, 'fixture')
                    """
                ),
                {"id": theme_id, "name": f"fixture-{theme_id}"},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO investment_theses (
                        id, theme_id, company, symbol, claim, status, horizon,
                        created_at, updated_at
                    ) VALUES (
                        :id, :theme_id, 'Fixture', 'FIX', 'Fixture claim',
                        'active', 'multi_year', :created_at, :created_at
                    )
                    """
                ),
                {
                    "id": thesis_id,
                    "theme_id": theme_id,
                    "created_at": created_at,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO investment_thesis_forecasts (
                        id, thesis_id, forecast_key, forecast_type, direction,
                        target_value, target_date, as_of, created_at
                    ) VALUES (
                        :id, :thesis_id, :key, 'price', 'up', 100,
                        :target_date, :created_at, :created_at
                    )
                    """
                ),
                {
                    "id": forecast_id,
                    "thesis_id": thesis_id,
                    "key": f"fixture-{forecast_id}",
                    "target_date": target_date,
                    "created_at": created_at,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO market_data (
                        symbol, timeframe, timestamp, open, high, low, close,
                        source, created_at, updated_at
                    ) VALUES (
                        'FIX', 'PRICE', :timestamp, 100, 111, 99, 110,
                        'fixture', :timestamp, :timestamp
                    )
                    """
                ),
                {
                    "timestamp": datetime.combine(
                        target_date,
                        datetime.min.time(),
                        tzinfo=UTC,
                    )
                },
            )
        draft = QuestionDraft(
            candidate=QuestionCandidate(
                origin_kind="forecast_resolution",
                question_type="forecast_resolution",
                atomic_question="Did the fixture forecast resolve at its boundary?",
                target_kind="forecast",
                target_ref=str(forecast_id),
                accepted_cutoff=NOW,
                required_evidence_shape={"outcome": "terminal price"},
                acceptable_source_families=("market_price",),
            ),
            priority=PriorityInputs(
                materiality=Decimal("0.9"),
                uncertainty=Decimal("0.8"),
                discrimination_power=Decimal("0.9"),
                urgency=Decimal("1"),
                freshness_gap=Decimal("1"),
                resolvability=Decimal("1"),
                expected_cost_usd=Decimal("0"),
                expected_runtime_seconds=30,
            ),
            not_before=NOW,
            due_at=NOW,
            expires_at=NOW + timedelta(days=7),
        )
        with self.get_session(self.config) as session:
            question = upsert_question(session, draft)
        plan = self._run()
        self.assertEqual(plan.selected_count, 1)
        with self.engine.connect() as connection:
            work_order_id = connection.execute(
                text(
                    "SELECT id FROM research_work_orders "
                    "WHERE question_id = :question_id"
                ),
                {"question_id": question["id"]},
            ).scalar_one()
        with self.get_session(self.config) as session:
            execution = execute_work_order(
                session,
                work_order_id=work_order_id,
                worker_id="postgres-test-worker",
                config=self.config,
            )
        self.assertEqual(execution["status"], "completed")
        self.assertEqual(execution["result_status"], "resolved")
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                    SELECT q.status AS question_status, w.status AS work_status,
                           b.status AS budget_status, o.status AS outcome_status,
                           o.actual_value, e.effect_type, e.material,
                           a.horizon_context, a.outcome_status AS attributed_status
                    FROM research_work_orders w
                    JOIN research_questions q ON q.id = w.question_id
                    JOIN budget_reservations b ON b.id = w.budget_reservation_id
                    JOIN research_effects e ON e.work_order_id = w.id
                    JOIN research_outcome_attributions a ON a.work_order_id = w.id
                    JOIN investment_forecast_outcomes o
                      ON o.id = a.forecast_outcome_id
                    WHERE w.id = :work_order_id
                    """
                    ),
                    {"work_order_id": work_order_id},
                )
                .mappings()
                .one()
            )
        self.assertEqual(row["question_status"], "resolved")
        self.assertEqual(row["work_status"], "completed")
        self.assertEqual(row["budget_status"], "settled")
        self.assertEqual(row["outcome_status"], "hit")
        self.assertEqual(row["actual_value"], 110)
        self.assertEqual(row["effect_type"], "forecast")
        self.assertTrue(row["material"])
        self.assertEqual(row["horizon_context"], "multi_year")
        self.assertEqual(row["attributed_status"], "hit")

    def test_daily_budget_prevents_second_plan_from_overspending(self):
        self._question("AAA")
        self._question("BBB")
        first = self._run()
        second = self._run()
        self.assertEqual(first.selected_count, 1)
        self.assertEqual(second.selected_count, 0)
        self.assertEqual(second.no_op_reason, "global_daily_budget_exceeded")
        with self.engine.connect() as connection:
            active = connection.execute(
                text(
                    "SELECT COUNT(*), COALESCE(SUM(estimated_usd), 0) "
                    "FROM budget_reservations WHERE status = 'active'"
                )
            ).one()
            work_count = connection.execute(
                text("SELECT COUNT(*) FROM research_work_orders")
            ).scalar_one()
        self.assertEqual(active[0], 1)
        self.assertLessEqual(float(active[1]), 0.15)
        self.assertEqual(work_count, 1)

    def test_two_planners_cannot_reserve_the_same_question(self):
        self._question("RACE")
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def worker():
            try:
                barrier.wait(timeout=5)
                results.append(self._run())
            except BaseException as exc:  # surfaced in the parent assertion
                failures.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(failures, failures)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(item.selected_count for item in results), 1)
        self.assertEqual(sum(item.coalesced for item in results), 1)
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text("SELECT COUNT(*) FROM research_work_orders")
                ).scalar_one(),
                1,
            )

    def test_equivalent_active_question_upserts_coalesce_concurrently(self):
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def worker(cutoff):
            draft = QuestionDraft(
                candidate=QuestionCandidate(
                    origin_kind="source_event",
                    question_type="evidence_refresh",
                    atomic_question="What changed for COALESCE?",
                    target_kind="entity",
                    target_ref="COALESCE",
                    accepted_cutoff=cutoff,
                    required_evidence_shape={"answer": "cited change"},
                    acceptable_source_families=("issuer_filing",),
                ),
                priority=PriorityInputs(
                    materiality=Decimal("0.8"),
                    uncertainty=Decimal("0.7"),
                    discrimination_power=Decimal("0.9"),
                    urgency=Decimal("0.8"),
                    freshness_gap=Decimal("1"),
                    resolvability=Decimal("0.8"),
                    expected_cost_usd=Decimal("0.10"),
                    expected_runtime_seconds=30,
                ),
                not_before=cutoff,
                due_at=cutoff + timedelta(days=3),
                expires_at=cutoff + timedelta(days=30),
            )
            try:
                barrier.wait(timeout=5)
                with self.get_session(self.config) as session:
                    results.append(dict(upsert_question(session, draft)))
            except BaseException as exc:  # surfaced in the parent assertion
                failures.append(exc)

        threads = [
            threading.Thread(target=worker, args=(NOW,)),
            threading.Thread(target=worker, args=(NOW,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertFalse(failures, failures)
        self.assertEqual(len(results), 2)
        self.assertEqual({row["id"] for row in results}, {results[0]["id"]})
        with self.engine.connect() as connection:
            count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM research_questions "
                    "WHERE target_ref = 'COALESCE' "
                    "AND status IN ('pending', 'planned', 'queued', 'running')"
                )
            ).scalar_one()
        self.assertEqual(count, 1)

    def test_newer_cutoff_persists_pending_successor_to_active_work(self):
        first_id = self._question("SUCCESSOR")
        self._run()
        newer = self._draft(
            "SUCCESSOR",
            cutoff=NOW + timedelta(minutes=1),
        )
        with self.get_session(self.config) as session:
            successor = upsert_question(session, newer)
        self.assertIsNotNone(successor)
        self.assertNotEqual(successor["id"], first_id)
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                    SELECT id, status, accepted_cutoff
                    FROM research_questions
                    WHERE question_key = :question_key
                    ORDER BY accepted_cutoff
                    """
                    ),
                    {"question_key": successor["question_key"]},
                )
                .mappings()
                .all()
            )
        self.assertEqual(
            [(row["status"], row["accepted_cutoff"]) for row in rows],
            [
                ("queued", NOW),
                ("pending", NOW + timedelta(minutes=1)),
            ],
        )

    def test_budget_admission_uses_execution_day_not_historical_cutoff(self):
        historical_cutoff = datetime.now(UTC) - timedelta(days=1)
        with self.get_session(self.config) as session:
            upsert_question(
                session,
                self._draft("DELAYED", cutoff=historical_cutoff),
            )
            result = run_planner(
                session,
                self.config,
                correlation_id=uuid4(),
                trigger_kind="scheduled",
                accepted_cutoff=historical_cutoff,
            )
        self.assertEqual(result.selected_count, 1)
        with self.engine.connect() as connection:
            budget_day = connection.execute(
                text("SELECT budget_day FROM budget_reservations")
            ).scalar_one()
        self.assertEqual(budget_day, datetime.now(UTC).date())
        self.assertNotEqual(budget_day, historical_cutoff.date())

    def test_final_attempt_lease_recovery_mirrors_research_terminal_state(self):
        self._question("FINAL-LEASE")
        result = self._run()
        work_order_id = result.work_order_ids[0]
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE analysis_jobs j
                    SET state = 'leased',
                        attempt_count = max_attempts,
                        claimed_by = 'dead-worker',
                        lease_expires_at = NOW() - INTERVAL '1 minute'
                    FROM research_work_orders w
                    WHERE w.id = :work_order_id
                      AND j.id = w.analysis_job_id
                    """
                ),
                {"work_order_id": work_order_id},
            )
            connection.execute(
                text(
                    """
                    UPDATE research_work_orders
                    SET status = 'leased', worker_id = 'dead-worker'
                    WHERE id = :work_order_id
                    """
                ),
                {"work_order_id": work_order_id},
            )
        with self.get_session(self.config) as session:
            self.assertEqual(reconcile_jobs(session, 10), 1)
            self.assertEqual(
                reconcile_terminal_work_order_failures(session, limit=10),
                1,
            )
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                    SELECT j.state AS job_state, w.status AS work_status,
                           q.status AS question_status, b.status AS budget_status
                    FROM research_work_orders w
                    JOIN analysis_jobs j ON j.id = w.analysis_job_id
                    JOIN research_questions q ON q.id = w.question_id
                    JOIN budget_reservations b ON b.id = w.budget_reservation_id
                    WHERE w.id = :work_order_id
                    """
                    ),
                    {"work_order_id": work_order_id},
                )
                .mappings()
                .one()
            )
        self.assertEqual(
            dict(row),
            {
                "job_state": "failed_terminal",
                "work_status": "failed_terminal",
                "question_status": "unresolvable",
                "budget_status": "settled",
            },
        )

    def test_manual_reasons_share_one_debounced_planner_job(self):
        first = enqueue_planner_job(
            self.config,
            trigger_kind="manual",
            trigger_ref="first operator reason",
            dedupe_ref="global",
            accepted_cutoff=NOW,
            correlation_id=uuid4(),
        )
        second = enqueue_planner_job(
            self.config,
            trigger_kind="manual",
            trigger_ref="different operator reason",
            dedupe_ref="global",
            accepted_cutoff=NOW,
            correlation_id=uuid4(),
        )
        self.assertTrue(first["created"])
        self.assertTrue(second["coalesced"])
        self.assertEqual(first["job_id"], second["job_id"])
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM analysis_jobs "
                        "WHERE job_type = 'research_planner'"
                    )
                ).scalar_one(),
                1,
            )

    def test_skill_registry_rejects_non_object_output_schema(self):
        with self.assertRaises(DBAPIError), self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO research_skill_versions (
                        skill_key, version, supported_question_types,
                        input_schema, output_schema, allowed_tools,
                        allowed_source_families, point_in_time_requirements,
                        model_allowed, model_policy, maximum_cost_usd,
                        maximum_runtime_seconds, maximum_attempts, validators,
                        promotion_status, content_fingerprint
                    ) VALUES (
                        'fixture.invalid_output', 1,
                        ARRAY['evidence_refresh'], '{}'::JSONB, '[]'::JSONB,
                        ARRAY[]::TEXT[], ARRAY['issuer_filing'],
                        '{}'::JSONB, FALSE, '{}'::JSONB, 0, 30, 1,
                        ARRAY['schema'], 'draft', :fingerprint
                    )
                    """
                ),
                {"fingerprint": "f" * 64},
            )

    def test_exact_terminal_question_replay_is_idempotent(self):
        draft = self._draft("REPLAY")
        with self.get_session(self.config) as session:
            first = upsert_question(session, draft)
        self.assertIsNotNone(first)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE research_questions
                    SET status = 'unresolvable', unresolved_reason = 'fixture terminal'
                    WHERE id = :id
                    """
                ),
                {"id": first["id"]},
            )
        with self.get_session(self.config) as session:
            replay = upsert_question(session, draft)
        self.assertIsNotNone(replay)
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["status"], "unresolvable")
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM research_questions "
                        "WHERE target_ref = 'REPLAY'"
                    )
                ).scalar_one(),
                1,
            )

    def test_terminal_questions_and_accepted_plans_are_immutable(self):
        question_id = self._question("LOCKED")
        result = self._run()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE research_questions SET status = 'running' "
                    "WHERE id = :question_id"
                ),
                {"question_id": question_id},
            )
            connection.execute(
                text(
                    """
                    UPDATE research_work_orders
                    SET status = 'failed_terminal', error_kind = 'fixture'
                    WHERE question_id = :question_id
                    """
                ),
                {"question_id": question_id},
            )
            connection.execute(
                text(
                    """
                    UPDATE research_questions
                    SET status = 'unresolvable', unresolved_reason = 'fixture terminal'
                    WHERE id = :question_id
                    """
                ),
                {"question_id": question_id},
            )
        with self.assertRaises(DBAPIError), self.engine.begin() as connection:
            connection.execute(
                text("UPDATE research_questions SET status = 'pending' WHERE id = :id"),
                {"id": question_id},
            )
        with self.assertRaises(DBAPIError), self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE research_plans SET no_op_reason = 'tampered' WHERE id = :id"
                ),
                {"id": result.plan_id},
            )
        with self.assertRaises(DBAPIError), self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE research_plan_decisions SET reason_codes = ARRAY['tampered'] "
                    "WHERE plan_id = :id"
                ),
                {"id": result.plan_id},
            )


if __name__ == "__main__":
    unittest.main()
