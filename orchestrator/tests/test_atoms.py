import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atoms import (
    atom_history,
    current_atoms,
    expire_atoms,
    publish_atom,
    session_close_target,
    validate_evidence,
)
from processors.base import canonical_fingerprint

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
ATOM_ID = UUID("33333333-3333-4333-8333-333333333333")
PRIOR_ID = UUID("44444444-4444-4444-8444-444444444444")


class Result:
    def __init__(self, *, first=None, rows=None):
        self._first = first
        self._rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._rows


class Session:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.commit = MagicMock()

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        if not self.results:
            raise AssertionError(f"unexpected SQL call: {statement}")
        return self.results.pop(0)


def atom(**overrides):
    value = {
        "subject_type": "macro_series",
        "subject_id": "CPIAUCSL",
        "claim_type": "event_interpretation",
        "claim": "US consumer prices cooled relative to consensus.",
        "observation_text": "CPI printed below consensus.",
        "interpretation_text": "Disinflation continues.",
        "scenario_text": "Rate cuts remain data dependent.",
        "unknowns": ["revision risk"],
        "affected_assets": ["EURUSD"],
        "time_horizon": "48h",
        "confidence": 0.7,
        "confidence_components": {"data_quality": 0.9},
        "valid_from": NOW,
        "expires_at": NOW + timedelta(hours=48),
        "input_fingerprint": canonical_fingerprint({"case": "one"}),
    }
    value.update(overrides)
    return value


class AtomValidationTests(unittest.TestCase):
    def test_unknown_evidence_ids_fail_publication(self):
        session = Session(
            [
                Result(first=None),
                Result(first={"source_timestamp": NOW}),
            ]
        )
        resolved, errors = validate_evidence(
            session,
            [
                {
                    "evidence_type": "macro_series",
                    "evidence_id": "MISSING",
                    "relationship": "supports",
                },
                {
                    "evidence_type": "macro_series",
                    "evidence_id": "CPIAUCSL",
                    "relationship": "supports",
                },
                {
                    "evidence_type": "provider_secret",
                    "evidence_id": "x",
                    "relationship": "supports",
                },
            ],
        )
        self.assertEqual(len(resolved), 1)
        self.assertIn("unknown_evidence:macro_series:MISSING", errors)
        self.assertIn("unsupported_evidence_type:provider_secret", errors)
        failing = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "evidence validation failed"):
            publish_atom(
                failing,
                atom(),
                [
                    {
                        "evidence_type": "macro_series",
                        "evidence_id": "MISSING",
                        "relationship": "supports",
                    }
                ],
                now=NOW,
            )

    def test_publish_is_auditable_bounded_and_caller_owned(self):
        created = {**atom(), "id": ATOM_ID, "status": "published"}
        session = Session(
            [
                Result(first={"source_timestamp": NOW}),
                Result(first=created),
                Result(),
            ]
        )
        result = publish_atom(
            session,
            atom(),
            [
                {
                    "evidence_type": "macro_series",
                    "evidence_id": "CPIAUCSL",
                    "relationship": "supports",
                }
            ],
            now=NOW,
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["atom_id"], ATOM_ID)
        self.assertEqual(result["evidence"], 1)
        insert_sql = session.calls[1][0]
        self.assertIn("ON CONFLICT DO NOTHING RETURNING *", insert_sql)
        session.commit.assert_not_called()
        insert_params = session.calls[1][1]
        self.assertEqual(json.loads(insert_params["affected_assets"]), ["EURUSD"])
        self.assertEqual(
            json.loads(insert_params["confidence_components"]),
            {"data_quality": 0.9},
        )
        self.assertEqual(json.loads(insert_params["invalidation_conditions"]), [])

    def test_duplicate_fingerprint_is_a_noop_without_new_atom(self):
        session = Session([Result(first={"source_timestamp": NOW}), Result(first=None)])
        result = publish_atom(
            session,
            atom(),
            [
                {
                    "evidence_type": "macro_series",
                    "evidence_id": "CPIAUCSL",
                    "relationship": "supports",
                }
            ],
            now=NOW,
        )
        self.assertEqual(result["status"], "duplicate")
        self.assertIsNone(result["atom_id"])

    def test_supersession_marks_prior_atom_and_requires_existing_target(self):
        created = {**atom(), "id": ATOM_ID, "status": "published"}
        session = Session(
            [
                Result(first={"source_timestamp": NOW}),
                Result(first={"id": PRIOR_ID, "status": "published"}),
                Result(first=created),
                Result(),
                Result(),
            ]
        )
        result = publish_atom(
            session,
            atom(
                input_fingerprint=canonical_fingerprint({"case": "two"}),
                supersedes_atom_id=PRIOR_ID,
            ),
            [
                {
                    "evidence_type": "macro_series",
                    "evidence_id": "CPIAUCSL",
                    "relationship": "supports",
                }
            ],
            now=NOW,
        )
        self.assertEqual(result["status"], "published")
        update_sql = session.calls[-1][0]
        self.assertIn("status = 'superseded'", update_sql)
        self.assertEqual(session.calls[-1][1]["id"], PRIOR_ID)

        session = Session([Result(first={"source_timestamp": NOW}), Result(first=None)])
        with self.assertRaisesRegex(ValueError, "does not exist"):
            publish_atom(
                session,
                atom(
                    input_fingerprint=canonical_fingerprint({"case": "three"}),
                    supersedes_atom_id=uuid4(),
                ),
                [
                    {
                        "evidence_type": "macro_series",
                        "evidence_id": "CPIAUCSL",
                        "relationship": "supports",
                    }
                ],
                now=NOW,
            )


class AtomExpiryTests(unittest.TestCase):
    def test_expired_superseded_and_retracted_atoms_stay_auditable(self):
        session = Session(
            [
                Result(rows=[{"id": ATOM_ID}]),
                Result(),
                Result(rows=[]),
                Result(rows=[]),
                Result(rows=[]),
            ]
        )
        result = expire_atoms(session, {"analysis_atoms": {}}, NOW)
        self.assertEqual(result["expired"], 1)
        update_sql = session.calls[1][0]
        self.assertIn("status = 'expired'", update_sql)
        self.assertIn("status IN ('draft', 'validated', 'published')", update_sql)

    def test_intraday_atoms_expire_at_configured_session_close(self):
        before = datetime(2026, 8, 6, 20, 0, tzinfo=UTC)
        after = datetime(2026, 8, 6, 22, 0, tzinfo=UTC)
        settings = {"intraday_session_close": "21:00:00"}
        self.assertEqual(session_close_target(before, settings).day, 6)
        self.assertEqual(session_close_target(after, settings).day, 7)

    def test_current_atoms_and_history_are_bounded_and_grouped(self):
        current = Session([Result(rows=[{"id": ATOM_ID, "status": "published"}])])
        rows = current_atoms(current, subject_type="macro_series", limit=9999)
        self.assertEqual(len(rows), 1)
        self.assertIn("LIMIT 20", current.calls[0][0])
        self.assertEqual(current.calls[0][1]["limit"], 200)
        history = Session(
            [
                Result(
                    rows=[
                        {"id": ATOM_ID, "status": "published"},
                        {"id": PRIOR_ID, "status": "superseded"},
                    ]
                )
            ]
        )
        audit = atom_history(history, "macro_series", "CPIAUCSL")
        self.assertEqual([row["status"] for row in audit], ["published", "superseded"])
        self.assertIn("LIMIT :limit", history.calls[0][0])


if __name__ == "__main__":
    unittest.main()
