"""Real-PostgreSQL integration tests for the catalyst immutability migration.

Env-gated by ``TEST_DATABASE_URL`` (see ``pg_support``): skipped locally when
unset, run in CI against a disposable, self-provisioned database.  Proves the
migration 054 contracts that mocks cannot: the conservative legacy stamp
(applied to every legacy row, never moving a timestamp backward), the
immutability trigger (every replay input frozen, updated_at included),
idempotent re-application, and the ``updated_at <= as_of`` replay cutoff the
evaluator relies on.  Also proves the autonomy cycle's concurrent
exact-identity catalyst guard: two transactions racing the same absent
``(thesis_id, description)`` persist exactly one row with truthful
changed/no-op outcomes, while distinct descriptions never serialize on the
identity lock.  The promote-vs-backfill interleaving on one thesis is
covered too: the backfill blocks on the fusion canonical-key lock before
the catalyst lock (global K before C order) instead of forming the
K→C/C→K deadlock that would abort a whole cycle.
"""

import sys
import threading
import time
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
from sqlalchemy.orm import Session

from thesis_autonomy import (
    _backfill_generated_catalysts,
    _ensure_candidate_catalyst,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CATALYST_IMMUTABILITY_MIGRATION = (
    REPOSITORY_ROOT / "db" / "migrations" / "054_catalyst_immutability.sql"
)

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
        # Best-effort guard restoration: a legacy test may have dropped the
        # trigger and then rolled back the migration application on
        # failure, leaving the guard absent for later tests.  Re-applying
        # the migration first is a no-op when the guard already exists and
        # re-creates it when it does not; the data is cleaned afterwards.
        # This never masks a real assertion failure in the test itself.
        with self.engine.begin() as connection:
            connection.execute(text(CATALYST_IMMUTABILITY_MIGRATION.read_text()))
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

    def _seed_thesis(self) -> str:
        """Insert one theme and thesis; returns the thesis id."""
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
                    "name": f"catalyst-concurrency-{theme_id}",
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
        return thesis_id

    def _seed_legacy_thesis(self, *, canonical_key: str, catalyst_summary: str) -> str:
        """Insert one legacy fusion thesis with a summary to backfill."""
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
                    "name": f"catalyst-interleave-{theme_id}",
                    "definition": "test theme",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO investment_theses "
                    "(id, theme_id, claim, origin, canonical_key, "
                    "catalyst_summary) "
                    "VALUES (:id, :theme_id, :claim, 'fusion', :canonical_key, "
                    ":catalyst_summary)"
                ),
                {
                    "id": thesis_id,
                    "theme_id": theme_id,
                    "claim": "test thesis",
                    "canonical_key": canonical_key,
                    "catalyst_summary": catalyst_summary,
                },
            )
        return thesis_id

    def _expect_rejected(self, statement, params):
        with self.assertRaises(SQLAlchemyError):
            with self.engine.begin() as connection:
                connection.execute(text(statement), params)

    def _reset_to_pre_migration_state(self, catalyst_id, *, reset_updated_at=True):
        """Drop the guard and restore the pre-054 legacy row state.

        Commits in its own transaction so later expected failures start
        from a clean (non-aborted) transaction.  By default updated_at is
        collapsed onto created_at (an untouched legacy row); pass
        reset_updated_at=False to keep a later updated_at (a legacy row
        that was mutated before the migration).
        """
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "DROP TRIGGER IF EXISTS investment_catalysts_immutable "
                    "ON investment_catalysts"
                )
            )
            connection.execute(
                text(
                    "DROP FUNCTION IF EXISTS enforce_investment_catalyst_immutability()"
                )
            )
            if not reset_updated_at:
                return
            connection.execute(
                text(
                    "UPDATE investment_catalysts SET updated_at = created_at "
                    "WHERE id = CAST(:id AS UUID)"
                ),
                {"id": catalyst_id},
            )

    def test_replay_input_updates_and_deletes_are_rejected_after_migration(
        self,
    ):
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

    def test_legacy_rows_are_stamped_then_frozen_and_reapply_is_a_noop(self):
        created_at = datetime.now(UTC) - timedelta(days=30)
        catalyst_id = self._seed(created_at=created_at)
        # Simulate the pre-054 state: no guard, untouched legacy row.
        self._reset_to_pre_migration_state(catalyst_id)
        # First application: the conservative stamp runs once and the
        # guard is installed.
        with self.engine.begin() as connection:
            connection.execute(text(CATALYST_IMMUTABILITY_MIGRATION.read_text()))
            stamped = (
                connection.execute(
                    text(
                        "SELECT created_at, updated_at FROM investment_catalysts "
                        "WHERE id = CAST(:id AS UUID)"
                    ),
                    {"id": catalyst_id},
                )
                .mappings()
                .first()
            )
            self.assertEqual(stamped["created_at"], created_at)
            self.assertGreater(stamped["updated_at"], stamped["created_at"])
            # Re-application is a no-op: the guard exists, so the stamp is
            # skipped and the row keeps its original migration-time stamp.
            connection.execute(text(CATALYST_IMMUTABILITY_MIGRATION.read_text()))
            restamped = (
                connection.execute(
                    text(
                        "SELECT updated_at FROM investment_catalysts "
                        "WHERE id = CAST(:id AS UUID)"
                    ),
                    {"id": catalyst_id},
                )
                .mappings()
                .first()
            )
            self.assertEqual(restamped["updated_at"], stamped["updated_at"])
        # The one-time legacy stamp cannot be bypassed afterwards: moving
        # updated_at backward (toward the pre-migration insert time) or
        # forward (to NOW()) is rejected.
        self._expect_rejected(
            "UPDATE investment_catalysts SET updated_at = :at "
            "WHERE id = CAST(:id AS UUID)",
            {"at": created_at, "id": catalyst_id},
        )
        self._expect_rejected(
            "UPDATE investment_catalysts SET updated_at = NOW() "
            "WHERE id = CAST(:id AS UUID)",
            {"id": catalyst_id},
        )
        # Scoring mutations are now rejected too.
        self._expect_rejected(
            "UPDATE investment_catalysts SET state = 'expired' "
            "WHERE id = CAST(:id AS UUID)",
            {"id": catalyst_id},
        )

    def test_stamped_legacy_row_cannot_be_made_visible_to_an_earlier_cutoff(
        self,
    ):
        # Acceptance: a pre-migration catalyst stamped at migration time can
        # never be made visible to an earlier cutoff by moving updated_at
        # backward.  The trigger freezes the stamp, so the evaluator's
        # predicate (created_at <= as_of AND updated_at <= as_of) keeps the
        # row hidden from every cutoff before the migration ran.
        created_at = datetime.now(UTC) - timedelta(days=30)
        catalyst_id = self._seed(created_at=created_at)
        self._reset_to_pre_migration_state(catalyst_id)
        with self.engine.begin() as connection:
            connection.execute(text(CATALYST_IMMUTABILITY_MIGRATION.read_text()))
            stamped = (
                connection.execute(
                    text(
                        "SELECT updated_at FROM investment_catalysts "
                        "WHERE id = CAST(:id AS UUID)"
                    ),
                    {"id": catalyst_id},
                )
                .mappings()
                .first()
            )
            self.assertGreater(stamped["updated_at"], created_at)
        # Rolling the stamp back to the pre-migration insert time is
        # rejected.
        self._expect_rejected(
            "UPDATE investment_catalysts SET updated_at = :at "
            "WHERE id = CAST(:id AS UUID)",
            {"at": created_at, "id": catalyst_id},
        )
        # The row stays hidden from cutoffs before the migration ran and
        # remains visible from the migration time on.
        predicate = (
            "SELECT 1 FROM investment_catalysts "
            "WHERE id = CAST(:id AS UUID) "
            "AND created_at <= :as_of AND updated_at <= :as_of"
        )
        with self.engine.begin() as connection:
            hidden_before_migration = connection.execute(
                text(predicate),
                {
                    "id": catalyst_id,
                    "as_of": datetime.now(UTC) - timedelta(days=20),
                },
            ).first()
            self.assertIsNone(hidden_before_migration)
            visible_after_migration = connection.execute(
                text(predicate),
                {
                    "id": catalyst_id,
                    "as_of": datetime.now(UTC) + timedelta(days=1),
                },
            ).first()
            self.assertIsNotNone(visible_after_migration)

    def test_legacy_row_with_later_updated_at_is_stamped_too(self):
        # A legacy row whose updated_at was bumped after insert (but before
        # the migration) still has unknowable mutation history, so it is
        # stamped at migration time too: leaving the old updated_at in
        # place would make it visible to pre-migration cutoffs.
        created_at = datetime.now(UTC) - timedelta(days=30)
        catalyst_id = self._seed(
            created_at=created_at, updated_at=created_at + timedelta(days=10)
        )
        self._reset_to_pre_migration_state(catalyst_id, reset_updated_at=False)
        with self.engine.begin() as connection:
            connection.execute(text(CATALYST_IMMUTABILITY_MIGRATION.read_text()))
            stamped = (
                connection.execute(
                    text(
                        "SELECT created_at, updated_at FROM investment_catalysts "
                        "WHERE id = CAST(:id AS UUID)"
                    ),
                    {"id": catalyst_id},
                )
                .mappings()
                .first()
            )
            self.assertGreater(
                stamped["updated_at"], stamped["created_at"] + timedelta(days=10)
            )
        # The stamp cannot be rolled back to the pre-migration mutation
        # time afterwards.
        self._expect_rejected(
            "UPDATE investment_catalysts SET updated_at = :at "
            "WHERE id = CAST(:id AS UUID)",
            {"at": created_at + timedelta(days=10), "id": catalyst_id},
        )
        # Hidden from every pre-migration cutoff, including ones the old
        # updated_at would have admitted (5 days after the old mutation).
        predicate = (
            "SELECT 1 FROM investment_catalysts "
            "WHERE id = CAST(:id AS UUID) "
            "AND created_at <= :as_of AND updated_at <= :as_of"
        )
        with self.engine.begin() as connection:
            hidden = connection.execute(
                text(predicate),
                {
                    "id": catalyst_id,
                    "as_of": datetime.now(UTC) - timedelta(days=5),
                },
            ).first()
            self.assertIsNone(hidden)

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

    def test_concurrent_same_identity_inserts_persist_exactly_one_row(self):
        # Two transactions race the guarded helper on the same absent
        # exact identity.  The winner's row stays uncommitted while the
        # loser's helper call runs, so the loser cannot see the row and
        # must serialize on the identity advisory lock; only after the
        # winner commits does the loser's absence guard run, see the
        # winner's row, and report a truthful no-op.  Without the lock the
        # loser would see "no catalyst" and permanently insert a
        # duplicate.  The main thread holds the exact identity lock (the
        # helper re-acquires it re-entrantly in the same transaction), so
        # the loser provably blocks while the winner's row is uncommitted.
        thesis_id = self._seed_thesis()
        description = "Quarterly disclosure confirms the operating change"
        outcomes: dict[str, bool] = {}
        errors: list[Exception] = []
        loser_done = threading.Event()

        def loser():
            try:
                with self.engine.connect() as connection:
                    session = Session(connection)
                    outcomes["loser"] = _ensure_candidate_catalyst(
                        session, thesis_id, description
                    )
                    session.commit()
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                loser_done.set()

        with self.engine.connect() as connection:
            session = Session(connection)
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"catalyst_identity:{thesis_id}:{description}"},
            )
            outcomes["winner"] = _ensure_candidate_catalyst(
                session, thesis_id, description
            )
            self.assertTrue(outcomes["winner"])
            loser_thread = threading.Thread(target=loser)
            loser_thread.start()
            # The loser cannot pass the identity lock while the winner's
            # transaction is open, so it cannot have completed yet.
            self.assertFalse(loser_done.wait(timeout=1))
            session.commit()  # releases the lock; the loser now sees the row
            loser_thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(outcomes, {"winner": True, "loser": False})
        with self.engine.begin() as connection:
            rows = connection.execute(
                text(
                    "SELECT id FROM investment_catalysts "
                    "WHERE thesis_id = CAST(:thesis_id AS UUID) "
                    "AND description = :description"
                ),
                {"thesis_id": thesis_id, "description": description},
            ).all()
            self.assertEqual(len(rows), 1)

    def test_backfill_serializes_behind_promoted_catalyst_in_lock_order(self):
        # Interleaving coverage for the global lock order.  A promote-path
        # transaction holds the fusion canonical-key lock and the catalyst
        # identity lock with its catalyst row uncommitted, while a
        # concurrent cycle runs the real legacy backfill on the same
        # thesis.  The backfill's guarded helper acquires the fusion lock
        # BEFORE the catalyst identity lock, so it blocks on the fusion
        # lock and then truthfully no-ops once the winner commits.  Before
        # the K→C order fix the backfill held catalyst C and later
        # attempted fusion K for the same thesis, forming a K→C/C→K
        # deadlock that aborted a whole cycle.
        canonical_key = f"autonomy:{uuid4()}"
        summary = "Quarterly disclosure confirms the operating change"
        thesis_id = self._seed_legacy_thesis(
            canonical_key=canonical_key, catalyst_summary=summary
        )
        outcomes: dict[str, bool] = {}
        backfill_results: dict[str, tuple] = {}
        errors: list[Exception] = []
        p_inserted = threading.Event()
        b_started = threading.Event()
        b_done = threading.Event()
        p_release = threading.Event()

        def promote_path():
            try:
                with self.engine.connect() as connection:
                    session = Session(connection)
                    outcomes["promote"] = _ensure_candidate_catalyst(
                        session, thesis_id, summary
                    )
                    p_inserted.set()
                    p_release.wait(timeout=30)
                    session.commit()
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
                p_release.set()

        def backfill_path():
            try:
                p_inserted.wait(timeout=30)
                b_started.set()
                with self.engine.connect() as connection:
                    session = Session(connection)
                    # A future reference keeps the seeded legacy thesis
                    # (default NOW() timestamps) provably visible.
                    backfill_results["backfill"] = _backfill_generated_catalysts(
                        session, datetime.now(UTC) + timedelta(days=1)
                    )
                    # The cycle would later promote the same thesis; the
                    # fusion lock is already held reentrantly (acquired
                    # before the catalyst lock inside the guarded helper),
                    # so this cannot deadlock.
                    session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": canonical_key},
                    )
                    session.commit()
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                b_done.set()

        promote_thread = threading.Thread(target=promote_path)
        backfill_thread = threading.Thread(target=backfill_path)
        promote_thread.start()
        backfill_thread.start()
        self.assertTrue(p_inserted.wait(timeout=30))
        self.assertTrue(b_started.wait(timeout=30))
        # The backfill's guarded helper is blocked on the fusion
        # canonical-key lock while the winner's catalyst row is
        # uncommitted; it cannot have finished.
        self.assertFalse(b_done.wait(timeout=1))
        p_release.set()
        promote_thread.join(timeout=30)
        backfill_thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(outcomes, {"promote": True})
        self.assertEqual(backfill_results["backfill"], ())
        with self.engine.begin() as connection:
            rows = connection.execute(
                text(
                    "SELECT id FROM investment_catalysts "
                    "WHERE thesis_id = CAST(:thesis_id AS UUID) "
                    "AND description = :description"
                ),
                {"thesis_id": thesis_id, "description": summary},
            ).all()
            self.assertEqual(len(rows), 1)

    def test_concurrent_distinct_descriptions_do_not_serialize(self):
        # The lock is keyed by the exact identity, so a different
        # description for the same thesis must not wait on the first
        # transaction's lock: both rows persist concurrently and both
        # calls report a change.
        thesis_id = self._seed_thesis()
        outcomes: dict[str, bool] = {}
        errors: list[Exception] = []
        first_inserted = threading.Event()
        second_finished_at: list[float] = []
        first_committed_at: list[float] = []
        second_finished_at_event = threading.Event()

        def first_identity():
            try:
                with self.engine.connect() as connection:
                    session = Session(connection)
                    outcomes["first"] = _ensure_candidate_catalyst(
                        session, thesis_id, "First catalyst"
                    )
                    first_inserted.set()
                    second_finished_at_event.wait(timeout=30)
                    session.commit()
                    first_committed_at.append(time.monotonic())
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
                second_finished_at_event.set()

        def second_identity():
            try:
                first_inserted.wait(timeout=30)
                with self.engine.connect() as connection:
                    session = Session(connection)
                    outcomes["second"] = _ensure_candidate_catalyst(
                        session, thesis_id, "Second catalyst"
                    )
                    second_finished_at.append(time.monotonic())
                    second_finished_at_event.set()
                    session.commit()
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
                second_finished_at_event.set()

        first_thread = threading.Thread(target=first_identity)
        second_thread = threading.Thread(target=second_identity)
        first_thread.start()
        second_thread.start()
        self.assertTrue(first_inserted.wait(timeout=30))
        # The second identity completes while the first transaction is
        # still open (the first only commits after this event): a
        # wrongly-global lock would block it until the first commits.
        self.assertTrue(second_finished_at_event.wait(timeout=30))
        first_thread.join(timeout=30)
        second_thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(outcomes, {"first": True, "second": True})
        # Strictly before the first transaction's commit.
        self.assertLess(second_finished_at[0], first_committed_at[0])
        with self.engine.begin() as connection:
            rows = connection.execute(
                text(
                    "SELECT description FROM investment_catalysts "
                    "WHERE thesis_id = CAST(:thesis_id AS UUID)"
                ),
                {"thesis_id": thesis_id},
            ).all()
            self.assertEqual(
                {row[0] for row in rows},
                {"First catalyst", "Second catalyst"},
            )


if __name__ == "__main__":
    unittest.main()
