"""Real-PostgreSQL contract for migration 057: unknown stays unknown.

Env-gated by ``TEST_DATABASE_URL`` exactly like the budget (045) and
reaction/analytics (044) integration tests.  The schema is rebuilt from
``db/init`` plus migrations 001..056 *only*, so the tests insert pre-057
rows and then apply 057 to observe the real upgrade semantics:

* never-evaluated rows (``last_evaluated_at IS NULL``) have their stored
  zero metrics backfilled to NULL, because an unevaluated thesis is
  *unknown*, not a favorable zero;
* evaluated rows are never rewritten — a legitimate evaluated zero is
  preserved byte-for-byte;
* snapshot sub-metrics become nullable while the frozen gated
  ``opportunity_score`` stays NOT NULL;
* ``budget_reservations`` admits zero-cost reservations (known-free
  models) and rejects negative estimates.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import UTC, datetime

from pg_support import (
    INIT_DIR,
    MIGRATIONS_DIR,
    REPO_ROOT,
    allow_reset_enabled,
    assert_safe_database,
    parse_config,
    require_postgres,
)
from sqlalchemy import text

THESIS_METRICS = (
    "evidence_strength",
    "contradiction_strength",
    "neglect_score",
    "catalyst_score",
    "confidence_score",
    "expected_value",
    "expected_shortfall",
    "opportunity_score",
)
SNAPSHOT_SUBMETRICS = (
    "evidence_strength",
    "contradiction_strength",
    "neglect_score",
    "catalyst_score",
    "confidence_score",
    "expected_value",
    "expected_shortfall",
)
MIGRATION_057 = REPO_ROOT / "db" / "migrations" / "057_thesis_metrics_nullable.sql"


class ThesisMetricsNullablePgTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = require_postgres()
        cls.config = parse_config(cls.url)
        assert_safe_database(cls.url, allow_reset=allow_reset_enabled())

        import migrate
        from db import get_engine

        cls.engine = get_engine(cls.config)
        with cls.engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        cls.engine.dispose()
        for path in sorted(INIT_DIR.glob("*.sql")):
            with cls.engine.begin() as connection:
                connection.execute(text(path.read_text()))

        # Apply migrations 001..056 only: the pre-057 world.  Rows inserted
        # now look exactly like production rows the forward migration will
        # encounter, defaults and all.
        with tempfile.TemporaryDirectory() as tmp:
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version <= 56:
                    shutil.copy2(path, tmp)
            migrate.MIGRATIONS_DIR = tmp
            migrate.run_migrations(cls.config)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE investment_themes, investment_theses, "
                    "investment_opportunity_snapshots, budget_reservations "
                    "RESTART IDENTITY CASCADE"
                )
            )

    def _create_thesis(self, connection, *, last_evaluated_at, metrics):
        theme_id = connection.exec_driver_sql(
            "INSERT INTO investment_themes (name, definition) "
            "VALUES ('upgrade-theme', 'disposable') "
            "ON CONFLICT (name) DO UPDATE SET definition = EXCLUDED.definition "
            "RETURNING id"
        ).fetchone()[0]
        columns = ", ".join(metrics)
        placeholders = ", ".join("%s" for _ in metrics)
        return connection.exec_driver_sql(
            f"INSERT INTO investment_theses "
            f"(theme_id, claim, last_evaluated_at, {columns}) "
            f"VALUES (%s, 'pre-057 row', %s, {placeholders}) RETURNING id",
            (theme_id, last_evaluated_at, *metrics.values()),
        ).fetchone()[0]

    def _metrics_row(self, connection, thesis_id):
        return connection.exec_driver_sql(
            f"SELECT {', '.join(THESIS_METRICS)} FROM investment_theses "
            f"WHERE id = %s",
            (thesis_id,),
        ).fetchone()

    def test_unevaluated_zeros_become_null_but_evaluated_zeros_survive(self):
        now = datetime.now(UTC)
        zeros = {column: 0 for column in THESIS_METRICS}
        nonzero = {
            "evidence_strength": 0.4,
            "contradiction_strength": 0.3,
            "neglect_score": 0.6,
            "catalyst_score": 0.8,
            "confidence_score": 0.7,
            "expected_value": 0.55,
            "expected_shortfall": 0.25,
            "opportunity_score": 0.75,
        }
        with self.engine.begin() as connection:
            unevaluated = self._create_thesis(
                connection, last_evaluated_at=None, metrics=zeros
            )
            evaluated_zero = self._create_thesis(
                connection, last_evaluated_at=now, metrics=zeros
            )
            evaluated_value = self._create_thesis(
                connection, last_evaluated_at=now, metrics=nonzero
            )
            connection.exec_driver_sql(MIGRATION_057.read_text())

        with self.engine.connect() as connection:
            unevaluated_row = self._metrics_row(connection, unevaluated)
            zero_row = self._metrics_row(connection, evaluated_zero)
            value_row = self._metrics_row(connection, evaluated_value)

        # Never evaluated -> every stored metric becomes unknown (NULL);
        # no favorable-zero fiction survives.
        self.assertTrue(
            all(value is None for value in unevaluated_row),
            f"unevaluated metrics should all be NULL: {unevaluated_row}",
        )
        # Evaluated rows are untouched, including a legitimate evaluated
        # zero (preserved exactly, not rewritten).
        self.assertEqual(zero_row, tuple(0 for _ in THESIS_METRICS))
        self.assertEqual(
            value_row,
            tuple(nonzero[column] for column in THESIS_METRICS),
        )

    def test_rerunning_057_is_a_noop(self):
        zeros = {column: 0 for column in THESIS_METRICS}
        with self.engine.begin() as connection:
            unevaluated = self._create_thesis(
                connection, last_evaluated_at=None, metrics=zeros
            )
            connection.exec_driver_sql(MIGRATION_057.read_text())
            # Second application: the backfill guard (only rows still
            # carrying stored values) plus DROP NOT NULL/DEFAULT and the
            # guarded constraint swap make this a no-op.
            connection.exec_driver_sql(MIGRATION_057.read_text())

        with self.engine.connect() as connection:
            row = self._metrics_row(connection, unevaluated)
        self.assertTrue(all(value is None for value in row))

    def test_snapshot_submetrics_become_nullable_but_gated_score_stays(self):
        from sqlalchemy.exc import IntegrityError

        with self.engine.begin() as connection:
            thesis_id = self._create_thesis(
                connection, last_evaluated_at=datetime.now(UTC),
                metrics={column: 0 for column in THESIS_METRICS},
            )
            connection.exec_driver_sql(MIGRATION_057.read_text())
            # A frozen snapshot whose sub-metrics were stored as zeros keeps
            # them; 057 never rewrites snapshot rows.
            snapshot_id = connection.exec_driver_sql(
                "INSERT INTO investment_opportunity_snapshots "
                "(thesis_id, snapshot_key, opportunity_score, "
                " expected_value, expected_shortfall, confidence_score, "
                " neglect_score, catalyst_score, evidence_strength, "
                " contradiction_strength) "
                "VALUES (%s, 'pre-057', 0.5, 0, 0, 0, 0, 0, 0, 0) RETURNING id",
                (thesis_id,),
            ).fetchone()[0]
            connection.exec_driver_sql(MIGRATION_057.read_text())
            stored = connection.exec_driver_sql(
                "SELECT opportunity_score, expected_value, evidence_strength "
                "FROM investment_opportunity_snapshots WHERE id = %s",
                (snapshot_id,),
            ).fetchone()
            self.assertEqual(tuple(stored), (0.5, 0, 0))

            # Post-057: unknown sub-metrics are storable as NULL…
            connection.exec_driver_sql(
                "INSERT INTO investment_opportunity_snapshots "
                "(thesis_id, snapshot_key, opportunity_score) "
                "VALUES (%s, 'unknown-submetrics', 0.4)",
                (thesis_id,),
            )
            null_row = connection.exec_driver_sql(
                "SELECT expected_value, confidence_score, neglect_score "
                "FROM investment_opportunity_snapshots "
                "WHERE snapshot_key = 'unknown-submetrics'"
            ).fetchone()
            self.assertEqual(tuple(null_row), (None, None, None))
            # …but the gated score is mandatory: every evaluation produces
            # a numeric opportunity score, so a snapshot without one is
            # rejected even after 057.
            with self.assertRaises(IntegrityError):
                connection.exec_driver_sql(
                    "INSERT INTO investment_opportunity_snapshots "
                    "(thesis_id, snapshot_key) VALUES (%s, 'no-gate')",
                    (thesis_id,),
                )

    def test_zero_estimate_reservation_admitted_negative_rejected(self):
        from sqlalchemy.exc import IntegrityError

        with self.engine.begin() as connection:
            connection.exec_driver_sql(MIGRATION_057.read_text())
            reserved_at = datetime.now(UTC)
            connection.exec_driver_sql(
                "INSERT INTO budget_reservations "
                "(budget_day, processor, estimated_usd, reserved_at, expires_at) "
                "VALUES (%s, 'thesis_autonomy', 0, %s, %s)",
                (
                    reserved_at.date().isoformat(),
                    reserved_at,
                    reserved_at.replace(hour=reserved_at.hour + 1),
                ),
            )
            zero_row = connection.exec_driver_sql(
                "SELECT processor, estimated_usd, status "
                "FROM budget_reservations"
            ).fetchone()
            self.assertEqual(tuple(zero_row), ("thesis_autonomy", 0, "active"))
            # The relaxed CHECK still rejects negative (below-cost) reserves:
            # the estimate can be zero only for a known-free model.
            with self.assertRaises(IntegrityError):
                connection.exec_driver_sql(
                    "INSERT INTO budget_reservations "
                    "(budget_day, processor, estimated_usd, reserved_at, expires_at) "
                    "VALUES (%s, 'thesis_autonomy', -0.01, %s, %s)",
                    (
                        reserved_at.date().isoformat(),
                        reserved_at,
                        reserved_at.replace(hour=reserved_at.hour + 1),
                    ),
                )


if __name__ == "__main__":
    unittest.main()
