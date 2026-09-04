"""Real-PostgreSQL tests for immutable catalyst replay inputs.

Env-gated by ``TEST_DATABASE_URL`` (see ``pg_support``): skipped locally when
unset, run in CI against a disposable, self-provisioned database. Proves the
authoritative schema freezes every catalyst replay input and that evaluator
cutoffs exclude rows not yet known at the requested time.
"""

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pg_support import (
    parse_config,
    provision,
    require_postgres,
    truncate,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

_CATALYST_TABLES = (
    "investment_catalysts",
    "investment_theses",
    "investment_themes",
)


class CatalystImmutabilityPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = require_postgres()
        cls.config = parse_config(cls.url)
        provision(cls.config)
        from db import get_engine

        cls.engine = get_engine(cls.config)

    def setUp(self):
        truncate(self.config, _CATALYST_TABLES)

    def _seed(self, *, created_at, updated_at=None):
        """Insert one theme, thesis, and catalyst; returns catalyst id.

        Commits in its own transaction so later expected failures start
        from a clean (non-aborted) transaction.
        """
        theme_id = str(uuid4())
        thesis_id = str(uuid4())
        catalyst_id = str(uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO investment_themes (id, name, definition) "
                    "VALUES (:id, :name, :definition)"
                ),
                {
                    "id": theme_id,
                    "name": f"catalyst-immutability-{theme_id}",
                    "definition": "test theme",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO investment_theses (id, theme_id, claim) "
                    "VALUES (:id, :theme_id, :claim)"
                ),
                {"id": thesis_id, "theme_id": theme_id, "claim": "test thesis"},
            )
            connection.execute(
                text(
                    "INSERT INTO investment_catalysts "
                    "(id, thesis_id, description, expected_at, state, "
                    "created_at, updated_at) "
                    "VALUES (:id, :thesis_id, :description, NULL, 'pending', "
                    ":created_at, :updated_at)"
                ),
                {
                    "id": catalyst_id,
                    "thesis_id": thesis_id,
                    "description": "Capex guide raise",
                    "created_at": created_at,
                    "updated_at": updated_at or created_at,
                },
            )
        return catalyst_id

    def _expect_rejected(self, statement, params):
        with self.assertRaises(SQLAlchemyError):
            with self.engine.begin() as connection:
                connection.execute(text(statement), params)

    def test_replay_input_updates_and_deletes_are_rejected(self):
        catalyst_id = self._seed(created_at=datetime.now(UTC) - timedelta(days=1))
        for statement, params in (
            (
                "UPDATE investment_catalysts SET description = :d "
                "WHERE id = CAST(:id AS UUID)",
                {"d": "rewritten", "id": catalyst_id},
            ),
            (
                "UPDATE investment_catalysts SET state = 'confirmed' "
                "WHERE id = CAST(:id AS UUID)",
                {"id": catalyst_id},
            ),
            (
                "UPDATE investment_catalysts SET expected_at = :at "
                "WHERE id = CAST(:id AS UUID)",
                {"at": datetime.now(UTC), "id": catalyst_id},
            ),
            (
                "UPDATE investment_catalysts SET created_at = :at "
                "WHERE id = CAST(:id AS UUID)",
                {"at": datetime.now(UTC), "id": catalyst_id},
            ),
            (
                "UPDATE investment_catalysts SET thesis_id = :tid "
                "WHERE id = CAST(:id AS UUID)",
                {"tid": str(uuid4()), "id": catalyst_id},
            ),
            (
                "UPDATE investment_catalysts SET updated_at = :at "
                "WHERE id = CAST(:id AS UUID)",
                {
                    "at": datetime.now(UTC) - timedelta(days=2),
                    "id": catalyst_id,
                },
            ),
            (
                "UPDATE investment_catalysts SET updated_at = :at "
                "WHERE id = CAST(:id AS UUID)",
                {"at": datetime.now(UTC) + timedelta(days=1), "id": catalyst_id},
            ),
            (
                "DELETE FROM investment_catalysts WHERE id = CAST(:id AS UUID)",
                {"id": catalyst_id},
            ),
        ):
            with self.subTest(statement=statement[:48]):
                self._expect_rejected(statement, params)
        # No bookkeeping is permitted either: updated_at cannot move
        # backward (widening visibility toward earlier cutoffs) or forward
        # (narrowing it), and the scoring row is bit-for-bit unchanged.
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT description, state, created_at, updated_at "
                        "FROM investment_catalysts "
                        "WHERE id = CAST(:id AS UUID)"
                    ),
                    {"id": catalyst_id},
                )
                .mappings()
                .first()
            )
            self.assertEqual(row["description"], "Capex guide raise")
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["updated_at"], row["created_at"])

    def test_replay_cutoff_excludes_later_mutated_rows(self):
        # The evaluator's catalyst predicate (created_at <= as_of AND
        # updated_at <= as_of) must exclude a row whose known mutation time
        # lies after the cutoff even when its insert predates it.
        created_at = datetime.now(UTC) - timedelta(days=30)
        updated_at = datetime.now(UTC) - timedelta(days=10)
        theme_id = str(uuid4())
        thesis_id = str(uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO investment_themes (id, name, definition) "
                    "VALUES (:id, :name, :definition)"
                ),
                {
                    "id": theme_id,
                    "name": f"cutoff-{theme_id}",
                    "definition": "test theme",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO investment_theses (id, theme_id, claim) "
                    "VALUES (:id, :theme_id, :claim)"
                ),
                {
                    "id": thesis_id,
                    "theme_id": theme_id,
                    "claim": "test thesis",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO investment_catalysts "
                    "(thesis_id, description, state, created_at, updated_at) "
                    "VALUES (CAST(:tid AS UUID), :description, 'pending', "
                    ":created_at, :updated_at)"
                ),
                {
                    "tid": thesis_id,
                    "description": "Capex guide raise",
                    "created_at": created_at,
                    "updated_at": updated_at,
                },
            )
            predicate = (
                "SELECT 1 FROM investment_catalysts "
                "WHERE thesis_id = CAST(:tid AS UUID) "
                "AND created_at <= :as_of AND updated_at <= :as_of"
            )
            visible_at_20_days = connection.execute(
                text(predicate),
                {
                    "tid": thesis_id,
                    "as_of": datetime.now(UTC) - timedelta(days=20),
                },
            ).first()
            self.assertIsNone(visible_at_20_days)
            visible_at_5_days = connection.execute(
                text(predicate),
                {
                    "tid": thesis_id,
                    "as_of": datetime.now(UTC) - timedelta(days=5),
                },
            ).first()
            self.assertIsNotNone(visible_at_5_days)


if __name__ == "__main__":
    unittest.main()
