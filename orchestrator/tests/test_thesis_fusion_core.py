"""Core thesis lifecycle, canonical key, group management, and reference claim tests."""

import sys
import unittest
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

ORCH_ROOT = Path(__file__).resolve().parents[1]
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from support_thesis_fusion import (  # noqa: E402
    BEAR_THESIS_ID,
    GROUP_ID,
    LINK_ID,
    NOW,
    THEME_ID,
    THESIS_ID,
    Result,
    Session,
)
from thesis_fusion import (  # noqa: E402
    add_group_membership,
    canonical_thesis_key,
    create_find_group,
    merge_or_create_thesis,
    remove_group_membership,
)


class CanonicalKeyTests(unittest.TestCase):
    def test_key_is_deterministic_and_sensitive_to_identity(self):
        key = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        same = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="nvidia corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        self.assertEqual(key, same)
        self.assertNotEqual(
            key,
            canonical_thesis_key(
                theme_id=str(THEME_ID),
                subject="Nvidia Corp",
                direction="short",
                horizon="multi_year",
                mechanism="AI capex compounds.",
            ),
        )
        self.assertNotEqual(
            key,
            canonical_thesis_key(
                theme_id=str(THEME_ID),
                subject="Nvidia Corp",
                direction="long",
                horizon="multi_year",
                mechanism="Valuation is stretched.",
            ),
        )
        self.assertNotEqual(
            key,
            canonical_thesis_key(
                theme_id=str(THEME_ID),
                subject="Nvidia Corp",
                direction="long",
                horizon="months",
                mechanism="AI capex compounds.",
            ),
        )
        self.assertNotEqual(
            key,
            canonical_thesis_key(
                theme_id=str(uuid4()),
                subject="Nvidia Corp",
                direction="long",
                horizon="multi_year",
                mechanism="AI capex compounds.",
            ),
        )

    def test_invalid_direction_raises(self):
        with self.assertRaisesRegex(ValueError, "unsupported direction"):
            canonical_thesis_key(
                theme_id=str(THEME_ID),
                subject="Nvidia Corp",
                direction="sideways",
                horizon="multi_year",
                mechanism="x",
            )


class GroupHelperTests(unittest.TestCase):
    def test_create_find_group_creates_then_finds(self):
        session = Session([Result(first={"id": GROUP_ID})])  # INSERT RETURNING id
        created = create_find_group(
            session, name="NVDA bull vs bear", description="Tournament."
        )
        self.assertEqual(created, {"id": str(GROUP_ID), "created": True})
        self.assertEqual(len(session.calls), 1)
        insert_sql = session.calls[0][0]
        self.assertIn("INSERT INTO investment_thesis_groups", insert_sql)
        self.assertIn("ON CONFLICT (name) DO NOTHING", insert_sql)
        self.assertIn("RETURNING id", insert_sql)
        session.commit.assert_not_called()

        session = Session(
            [
                Result(first=None),  # INSERT conflict, no row returned
                Result(first={"id": GROUP_ID}),  # SELECT fallback
            ]
        )
        found = create_find_group(session, name="NVDA bull vs bear")
        self.assertEqual(found, {"id": str(GROUP_ID), "created": False})
        self.assertEqual(len(session.calls), 2)
        self.assertIn("INSERT INTO investment_thesis_groups", session.calls[0][0])
        self.assertIn("FROM investment_thesis_groups", session.calls[1][0])
        session.commit.assert_not_called()

    def test_concurrent_same_name_creation_is_idempotent(self):
        # Lost the insert race to a committed winner: SELECT returns the
        # winner's row and the group is never duplicated.
        session = Session(
            [
                Result(first=None),  # INSERT conflict, no row returned
                Result(first={"id": GROUP_ID}),  # winner's row
            ]
        )
        result = create_find_group(session, name="NVDA bull vs bear")
        self.assertEqual(result, {"id": str(GROUP_ID), "created": False})
        self.assertEqual(len(session.calls), 2)
        session.commit.assert_not_called()

        # Lost the race to a winner that rolled back: the retry INSERT wins,
        # so the caller still gets a group.
        session = Session(
            [
                Result(first=None),  # INSERT conflict, no row returned
                Result(first=None),  # winner rolled back: SELECT misses
                Result(first={"id": GROUP_ID}),  # retry INSERT wins
            ]
        )
        result = create_find_group(session, name="NVDA bull vs bear")
        self.assertEqual(result, {"id": str(GROUP_ID), "created": True})
        self.assertEqual(len(session.calls), 3)
        self.assertIn("ON CONFLICT (name) DO NOTHING", session.calls[2][0])
        session.commit.assert_not_called()

    def test_unsupported_status_rejected_pre_insert(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported group status"):
            create_find_group(session, name="x", status="deleted")
        self.assertEqual(session.calls, [])


class MergeCreateThesisTests(unittest.TestCase):
    def test_create_writes_canonical_key_and_initial_version(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(first=None),  # input fingerprint lookup
                Result(first=None),  # canonical key lookup
                Result(first={"id": THESIS_ID}),  # thesis INSERT RETURNING id
                Result(first={"max_version": 0}),  # version max
                Result(),  # version INSERT
            ]
        )
        key = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            rationale="Founding rationale.",
            input_fingerprint="f" * 64,
        )
        self.assertEqual(result["id"], str(THESIS_ID))
        self.assertTrue(result["created"])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["canonical_key"], key)
        insert_params = session.calls[3][1]
        self.assertEqual(insert_params["canonical_key"], key)
        self.assertEqual(insert_params["direction"], "long")
        self.assertEqual(insert_params["origin"], "generated")
        self.assertEqual(insert_params["input_fingerprint"], "f" * 64)
        version_params = session.calls[5][1]
        self.assertEqual(version_params["version"], 1)
        self.assertEqual(version_params["changed_by"], "fusion")
        session.commit.assert_not_called()

    def test_merge_appends_version_and_preserves_status(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(  # canonical key lookup
                    first={
                        "id": THESIS_ID,
                        "claim": "Old claim.",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                    }
                ),
                Result(first={"max_version": 2}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            rationale="New filing evidence.",
        )
        self.assertEqual(result["id"], str(THESIS_ID))
        self.assertFalse(result["created"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["version"], 3)
        version_params = session.calls[3][1]
        self.assertEqual(version_params["version"], 3)
        self.assertEqual(version_params["changed_by"], "fusion")
        update_sql = session.calls[4][0]
        self.assertIn("UPDATE investment_theses", update_sql)
        self.assertNotIn("status", update_sql)
        session.commit.assert_not_called()

    def test_identical_candidate_is_idempotent_noop(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(  # canonical key lookup, identical content
                    first={
                        "id": THESIS_ID,
                        "claim": "AI capex compounds.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                    }
                ),
                Result(first={"max_version": 4}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.6,
        )
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["version"], 4)
        self.assertEqual(len(session.calls), 3)
        session.commit.assert_not_called()

    def test_bull_and_bear_are_distinct_theses(self):
        bull_key = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        bear_key = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="short",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        self.assertNotEqual(bull_key, bear_key)
        bull = merge_or_create_thesis(
            Session(
                [
                    Result(first={"present": 1}),
                    Result(first=None),  # canonical key lookup
                    Result(first={"id": THESIS_ID}),
                    Result(first={"max_version": 0}),
                    Result(),
                ]
            ),
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
        )
        bear = merge_or_create_thesis(
            Session(
                [
                    Result(first={"present": 1}),
                    Result(first=None),  # canonical key lookup
                    Result(first={"id": BEAR_THESIS_ID}),
                    Result(first={"max_version": 0}),
                    Result(),
                ]
            ),
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex peaks.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="short",
        )
        self.assertTrue(bull["created"])
        self.assertTrue(bear["created"])
        self.assertNotEqual(bull["id"], bear["id"])

    def test_input_fingerprint_merges_matching_candidate(self):
        key = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(  # fingerprint lookup hits the same thesis
                    first={
                        "id": THESIS_ID,
                        "claim": "Old claim.",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                        "canonical_key": key,
                    }
                ),
                Result(first={"max_version": 1}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
        )
        self.assertFalse(result["created"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["id"], str(THESIS_ID))

    def test_input_fingerprint_conflict_raises(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(  # fingerprint lookup with a different identity
                    first={
                        "id": THESIS_ID,
                        "claim": "x",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                        "canonical_key": "d" * 64,
                    }
                ),
            ]
        )
        with self.assertRaisesRegex(ValueError, "input_fingerprint conflicts"):
            merge_or_create_thesis(
                session,
                theme_id=str(THEME_ID),
                company="Nvidia Corp",
                claim="AI capex compounds.",
                horizon="multi_year",
                mechanism="AI capex compounds.",
                direction="long",
                input_fingerprint="f" * 64,
            )
        self.assertEqual(len(session.calls), 2)

    def test_unknown_theme_raises(self):
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown theme"):
            merge_or_create_thesis(
                session,
                theme_id=str(THEME_ID),
                company="Nvidia Corp",
                claim="AI capex compounds.",
            )
        session.commit.assert_not_called()


class FusionReferenceClaimTests(unittest.TestCase):
    """Accepted-reference guard (migration 055) on merge claims."""

    REFERENCE = NOW
    OLDER = NOW - timedelta(days=1)
    NEWER = NOW + timedelta(days=1)

    def _key(self) -> str:
        return canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )

    def test_new_thesis_persists_the_accepted_reference_at_creation(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # input fingerprint lookup
                Result(first=None),  # canonical key lookup
                Result(first={"id": THESIS_ID}),  # thesis INSERT RETURNING id
                Result(first={"max_version": 0}),  # version max
                Result(),  # version INSERT
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertTrue(result["created"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["version"], 1)
        insert_sql, insert_params = session.calls[4]
        self.assertEqual(insert_params["fusion_reference_at"], self.REFERENCE)
        self.assertIn("fusion_reference_at", insert_sql)
        # The accepted candidate fingerprint is persisted atomically with
        # the reference at creation.
        self.assertEqual(insert_params["fusion_candidate_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", insert_sql)
        session.commit.assert_not_called()

    def test_manual_create_keeps_the_reference_null(self):
        # No input_fingerprint: the fingerprint lookup never runs, so the
        # scripted queue starts at the canonical-key lookup.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(first=None),  # canonical key lookup
                Result(first={"id": THESIS_ID}),  # thesis INSERT RETURNING id
                Result(first={"max_version": 0}),  # version max
                Result(),  # version INSERT
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
        )
        self.assertTrue(result["created"])
        self.assertFalse(result["stale"])
        insert_sql, insert_params = session.calls[2]
        self.assertIn("INSERT INTO investment_theses", insert_sql)
        self.assertIsNone(insert_params["fusion_reference_at"])
        # The fingerprint pair is meaningful only under an accepted
        # reference: manual creations persist no candidate fingerprint.
        self.assertIsNone(insert_params["fusion_candidate_fingerprint"])
        # Manual merges never acquire the canonical-key advisory lock.
        self.assertFalse(
            any("pg_advisory_xact_lock" in sql for sql, _params in session.calls)
        )
        session.commit.assert_not_called()

    def test_autonomous_merge_claims_when_reference_is_not_stale(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, older accepted reference
                    first={
                        "id": THESIS_ID,
                        "claim": "Old claim.",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.OLDER,
                        "fusion_candidate_fingerprint": "a" * 64,
                    }
                ),
                Result(first={"max_version": 2}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row with the claim
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertTrue(result["changed"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["version"], 3)
        lookup_sql = session.calls[3][0]
        self.assertIn("FOR UPDATE", lookup_sql)
        self.assertIn("fusion_reference_at", lookup_sql)
        self.assertIn("fusion_candidate_fingerprint", lookup_sql)
        update_sql, update_params = session.calls[6]
        self.assertIn("UPDATE investment_theses", update_sql)
        self.assertIn("fusion_reference_at", update_sql)
        self.assertEqual(update_params["accepted_reference"], self.REFERENCE)
        # The claim atomically persists the proven candidate fingerprint.
        self.assertEqual(update_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", update_sql)
        self.assertNotIn("status", update_sql)
        session.commit.assert_not_called()

    def test_stale_reference_is_an_explicit_noop_without_any_write(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, newer accepted reference
                    first={
                        "id": THESIS_ID,
                        "claim": "Newer claim.",
                        "variant_perception": None,
                        "confidence": 0.8,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.NEWER,
                        "fusion_candidate_fingerprint": "n" * 64,
                    }
                ),
                Result(first={"max_version": 4}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="Older claim.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.2,
            input_fingerprint="o" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["version"], 4)
        self.assertEqual(result["id"], str(THESIS_ID))
        # No version append and no current-field mutation happened: the
        # only statements were the theme check, the advisory lock, the
        # fingerprint lookup, the canonical lookup, and the version probe
        # (the row-lock keyword "FOR UPDATE" is a SELECT, not an UPDATE).
        self.assertEqual(len(session.calls), 5)
        for sql, _params in session.calls:
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE")))
        session.commit.assert_not_called()

    def test_null_stored_reference_is_claimable_by_any_cycle(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, legacy row without a guard
                    first={
                        "id": THESIS_ID,
                        "claim": "Old claim.",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": None,
                        "fusion_candidate_fingerprint": None,
                    }
                ),
                Result(first={"max_version": 1}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row with the claim
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["stale"])
        self.assertTrue(result["changed"])
        update_sql, update_params = session.calls[6]
        self.assertEqual(update_params["accepted_reference"], self.REFERENCE)
        # The claim persists the pair: reference plus proven fingerprint.
        self.assertEqual(update_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", update_sql)
        session.commit.assert_not_called()

    def test_unchanged_autonomous_merge_still_advances_the_guard(self):
        # The cycle accepted the thesis at the incoming reference even
        # though the claim content is unchanged; the guard must advance so
        # an older cycle finishing later cannot write after it.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, identical content
                    first={
                        "id": THESIS_ID,
                        "claim": "AI capex compounds.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.OLDER,
                        "fusion_candidate_fingerprint": "a" * 64,
                    }
                ),
                Result(),  # monotonic claim UPDATE (guard pair only)
                Result(first={"max_version": 3}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.6,
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["version"], 3)
        claim_sql, claim_params = session.calls[4]
        self.assertIn("UPDATE investment_theses", claim_sql)
        self.assertIn("fusion_reference_at", claim_sql)
        self.assertNotIn("claim =", claim_sql)
        self.assertEqual(claim_params["accepted_reference"], self.REFERENCE)
        # The guard-only claim persists the proven fingerprint too.
        self.assertEqual(claim_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", claim_sql)
        session.commit.assert_not_called()

    def test_manual_merge_never_touches_the_reference_guard(self):
        # Non-autonomy callers keep the pre-055 behavior: they merge past
        # any guard and never write or reject on it.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(  # canonical key lookup, guarded thesis
                    first={
                        "id": THESIS_ID,
                        "claim": "Old claim.",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                    }
                ),
                Result(first={"max_version": 1}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
        )
        self.assertFalse(result["stale"])
        self.assertTrue(result["changed"])
        update_sql, update_params = session.calls[4]
        self.assertNotIn("fusion_reference_at", update_sql)
        self.assertNotIn("accepted_reference", update_params)
        # Manual merges never erase the guard pair either: the fingerprint
        # column is absent from the write, so an existing claim stays
        # proven.
        self.assertNotIn("fusion_candidate_fingerprint", update_sql)
        self.assertNotIn("accepted_fingerprint", update_params)
        # Manual merges never acquire the canonical-key advisory lock.
        self.assertFalse(
            any("pg_advisory_xact_lock" in sql for sql, _params in session.calls)
        )
        session.commit.assert_not_called()

    def test_identical_fingerprint_rerun_at_the_same_reference_is_resumable(self):
        # An identical-input rerun (same fingerprint) at the same cycle
        # reference proves it is the same accepted output, so it stays a
        # valid (non-stale) resumable merge: child-state writes may safely
        # be retried, and the claim UPDATE is a no-op (identical reference).
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(  # fingerprint lookup hits the same thesis
                    first={
                        "id": THESIS_ID,
                        "claim": "AI capex compounds.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                        "fusion_candidate_fingerprint": "f" * 64,
                    }
                ),
                Result(),  # monotonic claim UPDATE (no-op pair write)
                Result(first={"max_version": 1}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.6,
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertFalse(result["stale"])
        lookup_sql = session.calls[2][0]
        self.assertIn("FOR UPDATE", lookup_sql)
        claim_sql, claim_params = session.calls[3]
        self.assertEqual(claim_params["accepted_reference"], self.REFERENCE)
        # The resume re-asserts the identical proven fingerprint; the
        # IS DISTINCT FROM WHERE makes the equal-reference write a no-op.
        self.assertEqual(claim_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", claim_sql)
        self.assertIn("IS DISTINCT FROM", claim_sql)
        session.commit.assert_not_called()

    def test_accepted_reference_requires_a_nonblank_fingerprint(self):
        # An accepted-reference claim must prove WHICH candidate it is:
        # without a fingerprint, distinct model outputs could claim the
        # same reference interchangeably.  The merge fails before any
        # advisory lock, lookup, or write.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
            ]
        )
        with self.assertRaisesRegex(
            ValueError, "accepted_reference requires a nonblank candidate fingerprint"
        ):
            merge_or_create_thesis(
                session,
                theme_id=str(THEME_ID),
                company="Nvidia Corp",
                claim="AI capex compounds.",
                horizon="multi_year",
                mechanism="AI capex compounds.",
                direction="long",
                accepted_reference=self.REFERENCE,
            )
        # Only the theme probe ran: no advisory lock and no write.
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("pg_advisory_xact_lock", session.calls[0][0])
        self.assertFalse(
            session.calls[0][0].lstrip().upper().startswith(("INSERT", "UPDATE"))
        )
        session.commit.assert_not_called()

    def test_equal_reference_with_different_fingerprint_is_stale_noop(self):
        # The same accepted reference is already proven to a different
        # candidate output: only the first proven fingerprint is
        # authoritative at a reference, so this merge is a complete no-op
        # regardless of content -- the caller skips every child-state write.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, same reference, other output
                    first={
                        "id": THESIS_ID,
                        "claim": "Other output claim.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                        "fusion_candidate_fingerprint": "a" * 64,
                    }
                ),
                Result(first={"max_version": 3}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.2,
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["version"], 3)
        self.assertEqual(result["id"], str(THESIS_ID))
        # No version append and no current-field mutation: the only
        # statements were the theme check, the advisory lock, the
        # fingerprint lookup, the canonical lookup, and the version probe.
        self.assertEqual(len(session.calls), 5)
        for sql, _params in session.calls:
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE")))
        session.commit.assert_not_called()

    def test_equal_reference_with_unprovable_stored_fingerprint_is_stale_noop(self):
        # A legacy/NULL stored fingerprint cannot prove the incoming
        # candidate is the one that was accepted at the reference, so the
        # equal-reference claim fails closed: only a strictly newer
        # reference may claim the thesis.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, same reference, NULL proof
                    first={
                        "id": THESIS_ID,
                        "claim": "Legacy claim.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                        "fusion_candidate_fingerprint": None,
                    }
                ),
                Result(first={"max_version": 2}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.2,
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertTrue(result["stale"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["version"], 2)
        self.assertEqual(len(session.calls), 5)
        for sql, _params in session.calls:
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE")))
        session.commit.assert_not_called()

    def test_newer_reference_wins_even_with_a_different_fingerprint(self):
        # A strictly newer accepted reference claims and persists both
        # fields, even when the newer output's fingerprint differs from the
        # older stored one: reference order, not fingerprint equality,
        # decides across references.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, older reference other output
                    first={
                        "id": THESIS_ID,
                        "claim": "Older output claim.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.OLDER,
                        "fusion_candidate_fingerprint": "a" * 64,
                    }
                ),
                Result(first={"max_version": 2}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row with the claim
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertTrue(result["changed"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["version"], 3)
        update_sql, update_params = session.calls[6]
        self.assertEqual(update_params["accepted_reference"], self.REFERENCE)
        self.assertEqual(update_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", update_sql)
        session.commit.assert_not_called()

    def test_newer_reference_claims_legacy_row_with_null_fingerprint(self):
        # A legacy row whose reference was backfilled but whose fingerprint
        # is NULL is claimable by a strictly newer cycle: the newer cycle
        # stores the pair (reference + its own proven fingerprint).
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, backfilled guard, NULL proof
                    first={
                        "id": THESIS_ID,
                        "claim": "Legacy claim.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                        "fusion_candidate_fingerprint": None,
                    }
                ),
                Result(first={"max_version": 1}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row with the claim
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.NEWER,
        )
        self.assertFalse(result["stale"])
        self.assertTrue(result["changed"])
        update_sql, update_params = session.calls[6]
        self.assertEqual(update_params["accepted_reference"], self.NEWER)
        self.assertEqual(update_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", update_sql)
        session.commit.assert_not_called()

    def test_create_race_at_the_same_reference_keeps_only_the_first_fingerprint(self):
        # Two concurrent cycles at the same reference with different model
        # outputs: the advisory lock serializes the both-see-no-thesis
        # create race, the loser's lookups find the winner's row, and the
        # equal-reference different-fingerprint guard makes the loser a
        # stale no-op -- completion order cannot choose between outputs.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup: winner unknown yet
                Result(  # canonical key lookup: winner's row now committed
                    first={
                        "id": THESIS_ID,
                        "claim": "First output claim.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                        "fusion_candidate_fingerprint": "a" * 64,
                    }
                ),
                Result(first={"max_version": 1}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="Second output claim.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.2,
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertTrue(result["stale"])
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["id"], str(THESIS_ID))
        # The loser never inserts a competing canonical_key row and never
        # writes any claim or child state.
        self.assertEqual(len(session.calls), 5)
        for sql, _params in session.calls:
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE")))
        session.commit.assert_not_called()

    def test_autonomous_merge_locks_the_canonical_key_before_lookups(self):
        # The advisory lock closes the both-see-no-thesis create race: two
        # concurrent autonomous cycles for the same canonical identity
        # serialize here, so the loser waits and then finds (and claims or
        # rejects) the winner's row instead of failing on the unique index.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no thesis yet)
                Result(first=None),  # canonical key lookup (no thesis yet)
                Result(first={"id": THESIS_ID}),  # thesis INSERT RETURNING id
                Result(first={"max_version": 0}),  # version max
                Result(),  # version INSERT
            ]
        )
        key = self._key()
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertTrue(result["created"])
        lock_sql, lock_params = session.calls[1]
        self.assertIn("pg_advisory_xact_lock", lock_sql)
        self.assertIn("hashtextextended", lock_sql)
        self.assertEqual(lock_params, {"key": key})
        # The lock precedes both thesis lookups: the fingerprint lookup is
        # the next statement.
        self.assertIn("FROM investment_theses", session.calls[2][0])
        self.assertIn("WHERE input_fingerprint", session.calls[2][0])
        session.commit.assert_not_called()


class GroupMembershipTests(unittest.TestCase):
    def test_add_membership_is_idempotent(self):
        session = Session(
            [
                Result(first={"present": 1}),  # group exists
                Result(first={"present": 1}),  # thesis exists
                Result(first={"id": THESIS_ID}),  # thesis row locked
                Result(first=None),  # no active membership in this group
                Result(first=None),  # no membership in another group
                Result(),  # INSERT ON CONFLICT DO NOTHING
                Result(),  # UPDATE investment_theses SET group_id
            ]
        )
        added = add_group_membership(
            session, str(GROUP_ID), str(THESIS_ID), note="Bull leg."
        )
        self.assertTrue(added)
        lock_sql = session.calls[2][0]
        self.assertIn("FOR UPDATE", lock_sql)
        insert_sql = session.calls[5][0]
        self.assertIn("INSERT INTO investment_thesis_group_members", insert_sql)
        self.assertIn("ON CONFLICT DO NOTHING", insert_sql)
        update_sql = session.calls[6][0]
        self.assertIn("UPDATE investment_theses", update_sql)
        self.assertIn("SET group_id = CAST(:group_id AS UUID)", update_sql)
        self.assertEqual(
            session.calls[6][1],
            {"group_id": str(GROUP_ID), "thesis_id": str(THESIS_ID)},
        )
        session.commit.assert_not_called()

        session = Session(
            [
                Result(first={"present": 1}),
                Result(first={"present": 1}),
                Result(first={"id": THESIS_ID}),
                Result(first={"present": 1}),  # already active
            ]
        )
        self.assertFalse(add_group_membership(session, str(GROUP_ID), str(THESIS_ID)))
        self.assertEqual(len(session.calls), 4)

    def test_add_membership_rejects_other_active_group(self):
        # One active group per thesis: a thesis already member of another
        # group is rejected before any write, so the snapshot never diverges.
        session = Session(
            [
                Result(first={"present": 1}),  # group exists
                Result(first={"present": 1}),  # thesis exists
                Result(first={"id": THESIS_ID}),  # thesis row locked
                Result(first=None),  # no active membership in this group
                Result(first={"present": 1}),  # active in ANOTHER group
            ]
        )
        with self.assertRaisesRegex(
            ValueError, "thesis already belongs to another group"
        ):
            add_group_membership(session, str(GROUP_ID), str(THESIS_ID))
        self.assertEqual(len(session.calls), 5)
        session.commit.assert_not_called()

    def test_remove_membership_clears_group_snapshot(self):
        session = Session(
            [
                Result(first={"id": LINK_ID}),  # active membership found
                Result(),  # UPDATE membership SET removed_at
                Result(),  # UPDATE investment_theses SET group_id = NULL
            ]
        )
        removed = remove_group_membership(session, str(GROUP_ID), str(THESIS_ID))
        self.assertTrue(removed)
        membership_sql = session.calls[1][0]
        self.assertIn("UPDATE investment_thesis_group_members", membership_sql)
        self.assertIn("removed_at = NOW()", membership_sql)
        self.assertIn("removed_at IS NULL", membership_sql)
        snapshot_sql = session.calls[2][0]
        self.assertIn("UPDATE investment_theses", snapshot_sql)
        self.assertIn("SET group_id = NULL", snapshot_sql)
        self.assertIn("AND group_id = CAST(:group_id AS UUID)", snapshot_sql)
        self.assertEqual(
            session.calls[2][1],
            {"thesis_id": str(THESIS_ID), "group_id": str(GROUP_ID)},
        )
        session.commit.assert_not_called()

    def test_remove_membership_without_active_row_returns_false(self):
        session = Session([Result(first=None)])
        self.assertFalse(
            remove_group_membership(session, str(GROUP_ID), str(THESIS_ID))
        )
        session.commit.assert_not_called()

    def test_unknown_group_raises(self):
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown group"):
            add_group_membership(session, str(GROUP_ID), str(THESIS_ID))
        session.commit.assert_not_called()
