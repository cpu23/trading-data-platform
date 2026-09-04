"""Behavioral tests for investment thesis proposals and review queue repository."""

import os
import sys
import unittest
from pathlib import Path
from uuid import uuid4

ORCH_ROOT = Path(__file__).resolve().parents[1]
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

os.environ.update(
    {
        "STATE_DIR": "/tmp/trading-data-thesis-review-test-state",
        "CONFIG_DIR": str(ORCH_ROOT.parent / "config"),
        "DB_USER": "test",
        "DB_PASSWORD": "test",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_NAME": "trading_data",
    }
)

from support_thesis_fusion import NOW, THEME_ID, Result, Session
from thesis_fusion import (
    PROPOSAL_STATUSES,
    approve_thesis_proposal,
    create_thesis_proposal,
    get_thesis_proposal,
    list_thesis_proposals,
    reject_thesis_proposal,
    request_thesis_proposal_revision,
)


class ProposalReviewHarnessSession(Session):
    """Extended in-memory session simulating proposal SQL tables and lifecycle."""

    def __init__(self, results=None):
        super().__init__(results or [])
        self.proposals: dict[str, dict] = {}
        self.proposal_keys: dict[str, str] = {}
        self.theses: dict[str, dict] = {}
        self.theses_by_fingerprint: dict[str, str] = {}
        self.theses_by_key: dict[str, str] = {}
        self.thesis_versions: dict[str, list[dict]] = {}
        self.scenarios: list[dict] = []
        self.evidence: list[dict] = []
        self.risks: list[dict] = []
        self.catalysts: list[dict] = []
        self.groups: dict[str, dict] = {}
        self.group_members: list[dict] = []
        self.falsification_runs: dict[str, dict] = {}

    def execute(self, statement, params=None):
        sql = str(getattr(statement, "text", statement))
        if isinstance(params, list):
            self.calls.append((sql, [dict(p) for p in params]))
        else:
            params = dict(params or {})
            self.calls.append((sql, params))
        if "SELECT pg_advisory_xact_lock" in sql:
            return Result(first={"locked": True})

        if "INSERT INTO investment_thesis_proposals" in sql:
            p_key = params["proposal_key"]
            if p_key in self.proposal_keys:
                existing_id = self.proposal_keys[p_key]
                row = dict(self.proposals[existing_id])
                row["created"] = False
                return Result(first=row)
            p_id = str(uuid4())
            row = {
                "id": p_id,
                "proposal_key": p_key,
                "canonical_key": params["canonical_key"],
                "theme_id": params.get("theme_id"),
                "company": params.get("company"),
                "symbol": params.get("symbol"),
                "subject": params["subject"],
                "direction": params.get("direction", "neutral"),
                "horizon": params.get("horizon", "months"),
                "mechanism": params.get("mechanism"),
                "status": "pending_review",
                "payload": params.get("payload", "{}"),
                "evidence": params.get("evidence", "[]"),
                "scenarios": params.get("scenarios", "[]"),
                "scoring": params.get("scoring", "{}"),
                "challenge": params.get("challenge", "{}"),
                "diff": params.get("diff", "{}"),
                "matching_thesis_id": params.get("matching_thesis_id"),
                "materialized_thesis_id": None,
                "reviewer_id": None,
                "review_note": None,
                "reviewed_at": None,
                "parent_proposal_id": params.get("parent_proposal_id"),
                "revision_instructions": params.get("revision_instructions"),
                "accepted_reference": params.get("accepted_reference", NOW),
                "created_at": NOW,
                "updated_at": NOW,
                "created": True,
            }
            self.proposals[p_id] = row
            self.proposal_keys[p_key] = p_id
            return Result(first=row)

        if "SELECT" in sql and "FROM investment_thesis_proposals" in sql:
            if (
                "WHERE id = CAST(:key AS UUID)" in sql
                or "WHERE id = CAST(:id AS UUID)" in sql
            ):
                k = str(params.get("key") or params.get("id"))
                row = self.proposals.get(k)
                return Result(first=row)
            if "WHERE proposal_key = :key" in sql:
                k = params["key"]
                p_id = self.proposal_keys.get(k)
                row = self.proposals.get(p_id) if p_id else None
                return Result(first=row)
            # List query
            rows = list(self.proposals.values())
            if "status = ANY(:statuses)" in sql:
                st = params["statuses"]
                rows = [r for r in rows if r["status"] in st]
            if "symbol = :symbol" in sql:
                rows = [r for r in rows if r["symbol"] == params["symbol"]]
            if "theme_id = CAST(:theme_id AS UUID)" in sql:
                rows = [
                    r for r in rows if str(r["theme_id"]) == str(params["theme_id"])
                ]
            limit = params.get("limit", len(rows))
            offset = params.get("offset", 0)
            return Result(rows=rows[offset : offset + limit])

        if "UPDATE investment_thesis_proposals" in sql:
            p_id = str(params["id"])
            if p_id in self.proposals:
                row = self.proposals[p_id]
                if "status = 'approved'" in sql:
                    row["status"] = "approved"
                    row["materialized_thesis_id"] = params.get("thesis_id")
                    row["reviewer_id"] = params.get("reviewer_id")
                    row["review_note"] = params.get("review_note")
                    row["reviewed_at"] = params.get("reviewed_at", NOW)
                elif "status = 'rejected'" in sql:
                    row["status"] = "rejected"
                    row["reviewer_id"] = params.get("reviewer_id")
                    row["review_note"] = params.get("review_note")
                    row["reviewed_at"] = params.get("reviewed_at", NOW)
                elif "status = 'revision_requested'" in sql:
                    row["status"] = "revision_requested"
                    row["reviewer_id"] = params.get("reviewer_id")
                    row["revision_instructions"] = params.get("revision_instructions")
                    row["review_note"] = params.get("review_note")
                    row["reviewed_at"] = params.get("reviewed_at", NOW)
                row["updated_at"] = NOW
            return Result(first={"updated": True})

        # Theme queries
        if "FROM investment_themes" in sql:
            if "WHERE id = CAST(:id AS UUID)" in sql:
                return Result(first={"present": 1})
            if "WHERE name =" in sql:
                return Result(first={"id": str(THEME_ID)})
        if "INSERT INTO investment_themes" in sql:
            return Result(first={"id": str(THEME_ID)})

        # Thesis queries
        if "INSERT INTO investment_theses" in sql:
            t_id = str(uuid4())
            c_key = params.get("canonical_key")
            fp = params.get("input_fingerprint")
            row = {
                "id": t_id,
                "canonical_key": c_key,
                "input_fingerprint": fp,
                "theme_id": params.get("theme_id"),
                "company": params.get("company"),
                "symbol": params.get("symbol"),
                "subject": params.get("subject")
                or params.get("company")
                or params.get("symbol"),
                "claim": params.get("claim"),
                "variant_perception": params.get("variant_perception"),
                "direction": params.get("direction", "neutral"),
                "horizon": params.get("horizon", "months"),
                "mechanism": params.get("mechanism"),
                "origin": params.get("origin", "fusion"),
                "status": "candidate",
                "version": 1,
                "created_at": NOW,
                "updated_at": NOW,
            }
            self.theses[t_id] = row
            if c_key:
                self.theses_by_key[c_key] = t_id
            if fp:
                self.theses_by_fingerprint[fp] = t_id
            return Result(first=row)

        if "SELECT" in sql and "FROM investment_theses" in sql:
            if "WHERE input_fingerprint = :fingerprint" in sql:
                fp = params.get("fingerprint")
                t_id = self.theses_by_fingerprint.get(fp)
                row = self.theses.get(t_id) if t_id else None
                return Result(first=row)
            if (
                "WHERE canonical_key = :canonical_key" in sql
                or "WHERE canonical_key = :key" in sql
            ):
                ck = params.get("canonical_key") or params.get("key")
                t_id = self.theses_by_key.get(ck)
                row = self.theses.get(t_id) if t_id else None
                return Result(first=row)
            if (
                "WHERE id = CAST(:thesis_id AS UUID)" in sql
                or "WHERE id = CAST(:id AS UUID)" in sql
            ):
                t_id = str(params.get("thesis_id") or params.get("id"))
                row = self.theses.get(t_id)
                return Result(first=row)
            if "SELECT 1 AS present FROM investment_theses" in sql:
                return Result(first={"present": 1})
            return Result(first=None)

        if "UPDATE investment_theses" in sql:
            t_id = str(params.get("thesis_id") or params.get("id"))
            if t_id in self.theses:
                if "company" in params and params["company"]:
                    self.theses[t_id]["company"] = params["company"]
                if "symbol" in params and params["symbol"]:
                    self.theses[t_id]["symbol"] = params["symbol"]
                self.theses[t_id]["updated_at"] = NOW
            return Result(first={"updated": True})

        # Thesis versions
        if "INSERT INTO investment_thesis_versions" in sql:
            t_id = str(params.get("thesis_id") or params.get("id"))
            v = int(params.get("version", 1))
            row = dict(params)
            row["id"] = str(uuid4())
            row["thesis_id"] = t_id
            row["version"] = v
            self.thesis_versions.setdefault(t_id, []).append(row)
            if t_id in self.theses:
                self.theses[t_id]["version"] = v
            return Result(first={"version": v})
        if (
            "SELECT COALESCE(MAX(version)" in sql
            or "FROM investment_thesis_versions" in sql
        ):
            t_id = str(params.get("id") or params.get("thesis_id"))
            vers = [r.get("version", 1) for r in self.thesis_versions.get(t_id, [])]
            max_v = max(vers) if vers else 0
            if "COALESCE(MAX(version)" in sql:
                return Result(first={"max_version": max_v})
            rows = list(self.thesis_versions.get(t_id, []))
            rows.sort(key=lambda r: r.get("version", 1), reverse=True)
            if "LIMIT 1" in sql:
                return Result(first=rows[0] if rows else None)
            return Result(rows=rows)

        # Catalysts
        if "INSERT INTO investment_catalysts" in sql:
            cat_id = str(uuid4())
            self.catalysts.append(
                {"id": cat_id, "description": params.get("description")}
            )
            return Result(first={"id": cat_id})

        # Evidence
        if "FROM investment_thesis_evidence" in sql and "SELECT" in sql:
            t_id = (
                str(params.get("id") or params.get("thesis_id"))
                if isinstance(params, dict)
                else ""
            )
            ev_rows = [
                r for r in self.evidence if not t_id or str(r.get("thesis_id")) == t_id
            ]
            return Result(rows=ev_rows)
        if "INSERT INTO investment_evidence_items" in sql:
            return Result(first={"id": str(uuid4())})
        if "INSERT INTO investment_thesis_evidence" in sql:
            if isinstance(params, list):
                self.evidence.extend([dict(p) for p in params])
            elif isinstance(params, dict):
                self.evidence.append(dict(params))
            return Result(first={"attached": 1})
        # Scenarios
        if "FROM investment_thesis_scenarios" in sql and "SELECT" in sql:
            t_id = (
                str(params.get("id") or params.get("thesis_id"))
                if isinstance(params, dict)
                else ""
            )
            scs = [
                s for s in self.scenarios if not t_id or str(s.get("thesis_id")) == t_id
            ]
            if "name = :name" in sql and isinstance(params, dict) and "name" in params:
                scs = [s for s in scs if s.get("name") == params["name"]]
            if "is_base_case" in sql and (
                "is_base_case AND" in sql or "WHERE is_base_case" in sql
            ):
                scs = [s for s in scs if s.get("is_base_case")]
            if "LIMIT 1" in sql:
                return Result(first=scs[0] if scs else None)
            return Result(rows=scs)
        if "INSERT INTO investment_thesis_scenarios" in sql:
            sc_id = str(uuid4())
            sc_row = dict(params if isinstance(params, dict) else {})
            sc_row["id"] = sc_id
            sc_row["version"] = sc_row.get("version", 1)
            self.scenarios.append(sc_row)
            return Result(
                first={"id": sc_id, "changed": True, "version": sc_row["version"]}
            )
        if "UPDATE investment_thesis_scenarios" in sql:
            return Result(first={"updated": True})

        # Risks
        if "INSERT INTO investment_risks" in sql:
            r_id = str(uuid4())
            self.risks.append({"id": r_id, "description": params.get("description")})
            return Result(first={"id": r_id})

        # Groups
        if "INSERT INTO investment_thesis_groups" in sql:
            g_id = str(uuid4())
            g_name = params.get("name")
            self.groups[g_name] = {"id": g_id, "name": g_name}
            return Result(first={"id": g_id})
        if "SELECT id FROM investment_thesis_groups WHERE name = :name" in sql:
            g_name = params.get("name")
            row = self.groups.get(g_name)
            return Result(first=row or {"id": str(uuid4())})
        if "SELECT 1 AS present FROM investment_thesis_groups" in sql:
            return Result(first={"present": 1})
        if "INSERT INTO investment_thesis_group_members" in sql:
            self.group_members.append(dict(params))
            return Result(first={"id": str(uuid4())})
        if "FROM investment_thesis_group_members" in sql and "SELECT" in sql:
            return Result(first=None, rows=[])
        # Snapshots
        if (
            "investment_thesis_snapshots" in sql
            or "investment_opportunity_snapshots" in sql
        ):
            if "SELECT" in sql and "present" in sql:
                return Result(first=None)
            return Result(first={"id": str(uuid4()), "changed": True})
        # Falsification runs
        if "INSERT INTO investment_thesis_falsification_runs" in sql:
            fr_id = str(uuid4())
            self.falsification_runs[fr_id] = {"id": fr_id, "status": "in_progress"}
            return Result(first={"id": fr_id})
        if "FROM investment_thesis_falsification_runs" in sql and "SELECT" in sql:
            if "status" in sql:
                return Result(first={"status": "in_progress"})
            return Result(first=None)
        if "UPDATE investment_thesis_falsification_runs" in sql:
            return Result(first={"updated": True})

        # Evaluation / opportunities
        if (
            "SELECT t.id, t.subject, t.claim" in sql
            or "FROM investment_theses t WHERE t.id" in sql
        ):
            return Result(first=self.theses.get(str(params.get("id"))))
        if "FROM investment_catalysts" in sql and "SELECT" in sql:
            return Result(rows=[])
        if "FROM investment_thesis_positions" in sql and "SELECT" in sql:
            return Result(rows=[])
        if "SELECT 1 FROM investment_thesis_event_matches" in sql:
            return Result(first=None)
        if "SELECT close FROM market_data" in sql:
            return Result(first={"close": 100.0})
        return super().execute(statement, params)


class ThesisProposalDomainTests(unittest.TestCase):
    def setUp(self):
        self.session = ProposalReviewHarnessSession()

    def test_proposal_statuses_constant(self):
        self.assertIn("pending_review", PROPOSAL_STATUSES)
        self.assertIn("approved", PROPOSAL_STATUSES)
        self.assertIn("rejected", PROPOSAL_STATUSES)
        self.assertIn("revision_requested", PROPOSAL_STATUSES)

    def test_create_and_get_thesis_proposal(self):
        proposal = create_thesis_proposal(
            self.session,
            proposal_key="prop-1",
            canonical_key="canon-1",
            theme_id=str(THEME_ID),
            company="Acme Corp",
            symbol="ACME",
            subject="Acme Corp Margin Expansion",
            direction="long",
            horizon="months",
            mechanism="Operating leverage drives earnings beats.",
            payload={"claim": "Acme will beat consensus margins", "confidence": 0.85},
            evidence=[{"source_family": "filings", "excerpt": "Margins +200bps"}],
            scenarios=[{"name": "bull", "probability": 0.4, "expected_return": 15.0}],
            scoring={"opportunity_score": 0.75, "expected_value": 8.5},
            challenge={"state": "intact"},
            diff={"is_new": True},
        )
        self.assertTrue(proposal.get("created"))
        self.assertEqual(proposal["status"], "pending_review")
        self.assertEqual(proposal["subject"], "Acme Corp Margin Expansion")
        self.assertEqual(proposal["payload"]["confidence"], 0.85)
        self.assertEqual(proposal["scoring"]["opportunity_score"], 0.75)
        self.assertEqual(proposal["challenge"]["state"], "intact")

        # Get by UUID
        fetched = get_thesis_proposal(self.session, proposal["id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["id"], proposal["id"])
        self.assertEqual(fetched["proposal_key"], "prop-1")

        # Get by proposal_key
        fetched_key = get_thesis_proposal(self.session, "prop-1")
        self.assertIsNotNone(fetched_key)
        self.assertEqual(fetched_key["id"], proposal["id"])

    def test_replaying_same_proposal_key_is_idempotent(self):
        first = create_thesis_proposal(
            self.session,
            proposal_key="prop-idemp-1",
            canonical_key="canon-idemp-1",
            theme_id=str(THEME_ID),
            subject="Idempotent Proposal",
            direction="long",
            horizon="months",
        )
        self.assertTrue(first.get("created"))

        # Second creation with identical proposal_key returns existing
        second = create_thesis_proposal(
            self.session,
            proposal_key="prop-idemp-1",
            canonical_key="canon-idemp-1",
            theme_id=str(THEME_ID),
            subject="Idempotent Proposal",
            direction="long",
            horizon="months",
        )
        self.assertFalse(second.get("created"))
        self.assertEqual(second["id"], first["id"])

    def test_proposal_replay_cannot_mutate_immutable_staged_payload(self):
        first = create_thesis_proposal(
            self.session,
            proposal_key="prop-immutable-1",
            canonical_key="canon-immutable-1",
            theme_id=str(THEME_ID),
            company="Acme Corp",
            symbol="ACME",
            subject="Acme Margins Expansion",
            direction="long",
            horizon="months",
            mechanism="Original mechanism",
            payload={"claim": "Original claim", "confidence": 0.8},
            evidence=[{"source_family": "filings", "excerpt": "Original evidence"}],
            scenarios=[{"name": "bull", "probability": 0.5, "expected_return": 20.0}],
            scoring={"opportunity_score": 0.8},
            challenge={"state": "intact"},
        )
        self.assertTrue(first.get("created"))
        self.assertEqual(first["payload"]["claim"], "Original claim")

        # Second attempt with same proposal_key but altered payload/fields
        second = create_thesis_proposal(
            self.session,
            proposal_key="prop-immutable-1",
            canonical_key="canon-immutable-1",
            theme_id=str(THEME_ID),
            company="Tampered Corp",
            symbol="HACK",
            subject="Tampered Subject",
            direction="short",
            horizon="days",
            mechanism="Tampered mechanism",
            payload={"claim": "Tampered claim", "confidence": 0.1},
            evidence=[{"source_family": "news", "excerpt": "Tampered evidence"}],
            scenarios=[{"name": "bear", "probability": 0.9, "expected_return": -50.0}],
            scoring={"opportunity_score": 0.1},
            challenge={"state": "breached"},
        )
        self.assertFalse(second.get("created"))
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["company"], "Acme Corp")
        self.assertEqual(second["symbol"], "ACME")
        self.assertEqual(second["subject"], "Acme Margins Expansion")
        self.assertEqual(second["direction"], "long")
        self.assertEqual(second["horizon"], "months")
        self.assertEqual(second["mechanism"], "Original mechanism")
        self.assertEqual(second["payload"]["claim"], "Original claim")
        self.assertEqual(second["payload"]["confidence"], 0.8)
        self.assertEqual(second["evidence"][0]["excerpt"], "Original evidence")
        self.assertEqual(second["scenarios"][0]["name"], "bull")
        self.assertEqual(second["scoring"]["opportunity_score"], 0.8)
        self.assertEqual(second["challenge"]["state"], "intact")

    def test_list_thesis_proposals_filtering(self):
        create_thesis_proposal(
            self.session,
            proposal_key="p-a",
            canonical_key="c-a",
            theme_id=str(THEME_ID),
            symbol="AAPL",
            subject="Apple",
        )
        create_thesis_proposal(
            self.session,
            proposal_key="p-b",
            canonical_key="c-b",
            theme_id=str(THEME_ID),
            symbol="MSFT",
            subject="Microsoft",
        )

        all_props = list_thesis_proposals(self.session)
        self.assertEqual(len(all_props), 2)

        pending_props = list_thesis_proposals(self.session, status="pending_review")
        self.assertEqual(len(pending_props), 2)

        msft_props = list_thesis_proposals(self.session, symbol="MSFT")
        self.assertEqual(len(msft_props), 1)
        self.assertEqual(msft_props[0]["symbol"], "MSFT")

    def test_approve_thesis_proposal_materializes_canonical_records(self):
        proposal = create_thesis_proposal(
            self.session,
            proposal_key="prop-app-1",
            canonical_key="canon-app-1",
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            symbol="NVDA",
            subject="Nvidia Corp Capex Thesis",
            direction="long",
            horizon="months",
            mechanism="AI infrastructure demand expands GPU shipments.",
            payload={
                "claim": "AI datacenter capex compounds higher than expectations.",
                "variant_perception": "Street underestimates enterprise cluster ramp.",
                "catalyst": "Next-gen architecture product release.",
                "confidence": 0.9,
                "trend_context": "Shipments +45% YoY",
                "invalidators": ["Supply chain bottlenecks halt substrate delivery"],
            },
            evidence=[
                {
                    "evidence_type": "source_claim",
                    "evidence_id": "claim:capex-2026",
                    "relationship": "supports",
                    "source_family": "filings",
                    "source_name": "filings",
                    "origin_key": "sec:10q:nvda:2026q2",
                    "independence_key": "filings:nvda",
                    "evidence_fingerprint": "e" * 64,
                    "excerpt": "Datacenter revenue grew 150% year-over-year",
                    "source_timestamp": NOW.isoformat(),
                    "quality_score": 0.9,
                    "entailment_score": 0.85,
                }
            ],
            scenarios=[
                {
                    "name": "bull",
                    "description": "Accelerating ramp",
                    "probability": 0.4,
                    "expected_return": 25.0,
                },
                {
                    "name": "base",
                    "description": "Expected ramp",
                    "probability": 0.4,
                    "expected_return": 10.0,
                    "is_base_case": True,
                },
                {
                    "name": "bear",
                    "description": "Delayed adoption",
                    "probability": 0.2,
                    "expected_return": -15.0,
                },
            ],
            challenge={"findings": {"status": "clear"}},
        )

        approved = approve_thesis_proposal(
            self.session,
            proposal["id"],
            reviewer_id="reviewer_alice",
            review_note="Thorough evidence and robust scenario modeling.",
        )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["reviewer_id"], "reviewer_alice")
        self.assertEqual(
            approved["review_note"], "Thorough evidence and robust scenario modeling."
        )
        self.assertIsNotNone(approved["materialized_thesis_id"])

        # Check canonical thesis materialized in session
        materialized_id = approved["materialized_thesis_id"]
        self.assertIn(materialized_id, self.session.theses)
        thesis_row = self.session.theses[materialized_id]
        self.assertEqual(thesis_row["company"], "Nvidia Corp")
        self.assertEqual(thesis_row["symbol"], "NVDA")
        self.assertEqual(thesis_row["direction"], "long")
        self.assertEqual(thesis_row["origin"], "fusion")
        self.assertEqual(thesis_row["version"], 1)
        self.assertEqual(len(self.session.thesis_versions.get(materialized_id, [])), 1)
        # Second approval attempt should fail (only pending_review can transition)
        with self.assertRaises(ValueError) as ctx:
            approve_thesis_proposal(
                self.session, proposal["id"], reviewer_id="reviewer_bob"
            )
        self.assertIn("only pending_review can transition", str(ctx.exception))

    def test_reject_thesis_proposal_never_materializes(self):
        proposal = create_thesis_proposal(
            self.session,
            proposal_key="prop-rej-1",
            canonical_key="canon-rej-1",
            theme_id=str(THEME_ID),
            subject="Weak Thesis Proposal",
            direction="short",
            horizon="weeks",
            payload={"claim": "Weak claim without corroboration"},
        )

        rejected = reject_thesis_proposal(
            self.session,
            proposal["id"],
            reviewer_id="reviewer_charlie",
            review_note="Insufficient source evidence and vague mechanism.",
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reviewer_id"], "reviewer_charlie")
        self.assertIsNone(rejected["materialized_thesis_id"])

        # Terminal state: cannot approve or request revision after rejection
        with self.assertRaises(ValueError) as ctx:
            approve_thesis_proposal(
                self.session, proposal["id"], reviewer_id="reviewer_alice"
            )
        self.assertIn("only pending_review can transition", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            request_thesis_proposal_revision(
                self.session,
                proposal["id"],
                reviewer_id="reviewer_alice",
                revision_instructions="retry",
            )
        self.assertIn("only pending_review can transition", str(ctx.exception))

    def test_request_thesis_proposal_revision(self):
        proposal = create_thesis_proposal(
            self.session,
            proposal_key="prop-rev-1",
            canonical_key="canon-rev-1",
            theme_id=str(THEME_ID),
            company="Tesla Inc",
            symbol="TSLA",
            subject="Tesla Autonomy Thesis",
            direction="long",
            horizon="months",
            payload={
                "claim": "FSD adoption accelerates margin expansion",
                "confidence": 0.7,
            },
        )

        res = request_thesis_proposal_revision(
            self.session,
            proposal["id"],
            reviewer_id="reviewer_dan",
            revision_instructions="Please add regulatory risk invalidator and citation from peer filings.",
            review_note="Promising start but missing regulatory scrutiny.",
        )
        updated = res["proposal"]
        enqueue = res["enqueue_payload"]

        self.assertEqual(updated["status"], "revision_requested")
        self.assertEqual(updated["reviewer_id"], "reviewer_dan")
        self.assertEqual(
            updated["revision_instructions"],
            "Please add regulatory risk invalidator and citation from peer filings.",
        )

        self.assertEqual(enqueue["parent_proposal_id"], proposal["id"])
        self.assertEqual(enqueue["symbol"], "TSLA")
        self.assertEqual(
            enqueue["revision_instructions"],
            "Please add regulatory risk invalidator and citation from peer filings.",
        )
        self.assertEqual(enqueue["reviewer_id"], "reviewer_dan")
        self.assertEqual(enqueue["candidate_payload"]["confidence"], 0.7)

        # Cannot approve a revision-requested proposal directly
        with self.assertRaises(ValueError) as ctx:
            approve_thesis_proposal(
                self.session, proposal["id"], reviewer_id="reviewer_dan"
            )
        self.assertIn("only pending_review can transition", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
