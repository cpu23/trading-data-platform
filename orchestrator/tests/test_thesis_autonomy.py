"""Tests for the durable autonomous thesis-fusion cycle.

The cycle is exercised end to end with a credential-free injected runner,
challenger, and citation auditor against a content-routing in-memory fake
session (mirroring the queued-result conventions of test_thesis_fusion.py and
test_analysis_jobs.py).  No source-text assertions: every check defends an
observable contract — config bounds, deterministic evidence selection,
idempotent persistence, nullable scenarios, independent challenges,
contradiction attachment, breached->paused safety, role/challenger/auditor
failure isolation, position watch links, event-playbook persistence, forecast
freezing and outcome resolution, the production runner repair flow, durable
job dispatch, and the per-bucket event identity.
"""

import copy
import json
import os
import re
import sys
import unittest
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid5

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

os.environ.update(
    {
        "STATE_DIR": "/tmp/trading-data-thesis-autonomy-test-state",
        "DASHBOARD_USER": "test",
        "DASHBOARD_PASSWORD": "test",
        "DEPLOYMENT_MODE": "test",
        "LEGACY_BASIC_AUTH": "1",
        "CONFIG_DIR": str(ORCH_ROOT.parent / "config"),
        "DB_USER": "test",
        "DB_PASSWORD": "test",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_NAME": "trading_data",
        "FRED_API_KEY": "test",
        "OPENROUTER_API_KEY": "test",
        "OPENROUTER_MODEL": "test/model",
        "OANDA_API_KEY": "test",
        "SECRETS_FILE": "/nonexistent/test-secrets.env",
    }
)

from pydantic import ValidationError  # noqa: E402
from sqlalchemy import bindparam, create_engine, text  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from contracts.runtime_config import (  # noqa: E402
    AppConfig,
    ThesisAutonomyConfig,
)
from research_intelligence.contracts import (  # noqa: E402
    EvidenceSignal,
    NormalizedEntity,
    NormalizedEvidence,
    canonical_fingerprint,
)
from research_intelligence.evidence import (  # noqa: E402
    EvidenceCollection,
    EvidenceRegistry,
    exact_evidence_lookup,
)
from thesis_autonomy import (  # noqa: E402
    JOB_TYPE,
    LLMChallenger,
    LLMRoleRunner,
    LLMSemanticCitationAuditor,
    _attach_cited_evidence,
    _backfill_generated_catalysts,
    _backfill_missing_forecasts,
    _backfill_missing_market_identities,
    _candidate_evidence,
    _candidate_expected_at,
    _candidate_source_gate,
    _canonical_market_symbol,
    _close_at_or_before,
    _collect_evidence,
    _contradiction_signals,
    _count_unversioned_second_pass_candidates,
    _cycle_key,
    _ensure_candidate_catalyst,
    _load_second_pass_snapshot,
    _persist_candidate_risks,
    _resolve_matured_forecasts,
    _second_pass_candidates,
    _signal,
    _signal_from_row,
    enqueue_thesis_autonomy_job,
    run_autonomous_thesis_cycle,
    thesis_autonomy_identity,
)
from thesis_challenges import ChallengeProposal  # noqa: E402
from thesis_fusion import evaluate_thesis  # noqa: E402
from thesis_scoring import assess_evidence  # noqa: E402
from thesis_tournament import CITATION_FIELDS, role_output_schema  # noqa: E402

NOW = datetime(2026, 8, 15, 9, 30, tzinfo=UTC)
THEME_ID = "11111111-1111-4111-8111-111111111111"
EXISTING_ID = "23232323-2323-4232-8232-232323232323"
FIXED_NS = UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


def _id(seed: str) -> str:
    return str(uuid5(FIXED_NS, seed))


CANDIDATE = {
    "claim": "Strong evidence supports sustained margin expansion for the subject",
    "subject": "Acme Corporation",
    "instrument": "ACME",
    "direction": "long",
    "horizon": "months",
    "consensus": "The market consensus expects flat margins",
    "variant_perception": "The cited evidence shows costs are falling",
    "mechanism": "The cited evidence traces lower unit costs to the disclosed changes",
    "catalyst": "A quarterly disclosure confirming the change would force repricing",
    "trend_context": "The measured public market trend is improving",
    "valuation_context": "The filing analysis and public price provide valuation context",
    "sentiment_context": "Dated expectations and positioning support the variant view",
    "scenarios": {
        "bull": {
            "probability": 0.3,
            "expected_return": 0.1,
            "description": "disclosed cost trend accelerates and margins expand",
        },
        "base": {
            "probability": 0.5,
            "expected_return": 0.0,
            "description": "cost trend persists at the disclosed pace",
        },
        "bear": {
            "probability": 0.2,
            "expected_return": -0.2,
            "description": "cost trend reverses and margins compress",
        },
    },
    "invalidators": ["Cost reduction fails to materialize"],
    "missing_evidence": ["Forward guidance"],
    "confidence": 0.6,
}


def cycle_config(**overrides) -> dict:
    values = {
        "enabled": True,
        "lookback_days": 30,
        "maximum_evidence": 96,
        "maximum_promoted": 64,
        "maximum_challenges_per_run": 25,
        "event_debounce_minutes": 60,
        "model_budget_usd_per_run": 0.75,
        "max_output_tokens": 16384,
    }
    values.update(overrides)
    return {"thesis_autonomy": values, "llm": {"models": {"default": "test/model"}}}


def evidence_item(index: int, **overrides) -> NormalizedEvidence:
    adapters = (
        "investment_analyses",
        "public_equity_trends",
        "expectations_sentiment",
    )
    values = {
        "evidence_type": "source_claim",
        "evidence_id": f"claim:{index:04d}",
        "source_name": f"Source {index}",
        "source_timestamp": NOW - timedelta(days=1),
        "available_at": NOW - timedelta(days=1),
        "title": f"Evidence {index}",
        "bounded_excerpt": f"Disclosed cost trend for subject {index}",
        "provenance": {
            "adapter": adapters[index % len(adapters)],
            "source_family": f"family-{index}",
        },
        "point_in_time_safe": True,
        "entities": [
            NormalizedEntity.create("company", "acme-corporation"),
            NormalizedEntity.create("symbol", "ACME"),
        ],
    }
    values.update(overrides)
    return NormalizedEvidence.create(**values)


def evidence_items(count: int = 6) -> list[NormalizedEvidence]:
    return [evidence_item(index) for index in range(count)]


class ScriptedRunner:
    """Credential-free RoleRunner returning one fixed candidate per role."""

    def __init__(self, candidate: dict, fail_roles=()):
        self.candidate = candidate
        self.fail_roles = set(fail_roles)
        self.cost_usd = 0.0
        self.calls = 0
        self.errors: list[str] = []

    def run(self, *, role: str, prompt: str, schema: dict):
        self.calls += 1
        if role in self.fail_roles:
            raise RuntimeError("simulated role failure")
        self.cost_usd += 0.01
        refs = re.findall(r"^- (\S+) \|", prompt, re.MULTILINE)
        candidate = copy.deepcopy(self.candidate)
        candidate["evidence_refs"] = refs[:3]
        all_refs = list(candidate["evidence_refs"])
        trend_ref = next(
            (ref for ref in all_refs if ref.endswith("0001")),
            all_refs[0],
        )
        analysis_ref = next(
            (ref for ref in all_refs if ref.endswith("0000")),
            all_refs[0],
        )
        sentiment_ref = next(
            (ref for ref in all_refs if ref.endswith("0002")),
            all_refs[-1],
        )
        candidate["citations"] = {
            "claim": all_refs,
            "consensus": all_refs,
            "variant_perception": all_refs,
            "mechanism": all_refs,
            "catalyst": all_refs,
            "trend": [trend_ref],
            "valuation": list(dict.fromkeys([analysis_ref, trend_ref])),
            "sentiment": [sentiment_ref],
        }
        return [candidate]


class OpposingRunner(ScriptedRunner):
    """Emit one genuine short variant from the contrarian role."""

    def run(self, *, role: str, prompt: str, schema: dict):
        candidate = copy.deepcopy(self.candidate)
        if role == "contrarian":
            candidate["direction"] = "short"
            candidate["claim"] = (
                "Competitive pressure prevents the disclosed cost trend "
                "from producing durable margin expansion"
            )
            candidate["mechanism"] = (
                "Competition returns disclosed cost savings to customers"
            )
            candidate["variant_perception"] = (
                "Consensus overstates the persistence of current unit-cost gains"
            )
        original = self.candidate
        self.candidate = candidate
        try:
            return super().run(role=role, prompt=prompt, schema=schema)
        finally:
            self.candidate = original


class ScriptedChallenger:
    """Credential-free ChallengeRunner returning a fixed proposal or None."""

    def __init__(self, proposal: dict | None = None, fail: bool = False):
        self.proposal = proposal
        self.fail = fail
        self.cost_usd = 0.0
        self.calls = 0

    def challenge(self, snapshot, evidence):
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated challenger failure")
        if self.proposal is None:
            return None
        return ChallengeProposal.create(**self.proposal)


class RecordingChallenger(ScriptedChallenger):
    """ScriptedChallenger that records every (snapshot, evidence) pair."""

    def __init__(self, proposal: dict | None = None):
        super().__init__(proposal=proposal)
        self.captured: list[tuple] = []

    def challenge(self, snapshot, evidence):
        self.captured.append((snapshot, list(evidence)))
        return super().challenge(snapshot, evidence)


class ScriptedAuditor:
    """Credential-free SemanticCitationAuditor returning fixed verdicts."""

    def __init__(self, verdict: str = "entailed", fail: bool = False):
        self.verdict = verdict
        self.fail = fail
        self.cost_usd = 0.0
        self.calls = 0

    def audit(self, *, candidates, evidence):
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated auditor failure")
        decisions = []
        for candidate in candidates:
            decisions.append(
                {
                    "candidate_key": candidate["candidate_key"],
                    "verdict": self.verdict,
                    "cited_refs": list(candidate["evidence_refs"]),
                    "unsupported_claims": [],
                    "rationale": "deterministic test decision",
                }
            )
        return decisions


class Result:
    def __init__(self, first=None, rows=None, rowcount=0):
        self._first = first
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._rows


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


#: Exact-ID recovery routing: marker table name -> fake store attribute and
#: the request param names that identify rows (mirrors the production
#: recovery statements in research_intelligence/evidence.py).
_RECOVERY_STORE_TABLES = {
    "source_documents": "recovery_source_documents",
    "research_source_claims": "recovery_research_source_claims",
    "investment_filing_deltas": "recovery_filing_deltas",
    "investment_research_observations": "recovery_observations",
    "investment_analyses": "recovery_analyses",
    "market_data": "recovery_market_data",
    "corporate_actions": "recovery_corporate_actions",
    "positioning_reports": "recovery_positioning",
    "option_chain_snapshots": "recovery_option_snapshots",
    "story_clusters": "recovery_story_clusters",
}
_RECOVERY_ID_PARAMS = {
    "source_documents": ("ids",),
    "research_source_claims": ("ids",),
    "investment_filing_deltas": ("ids",),
    "investment_research_observations": ("ids",),
    "investment_analyses": ("ids",),
    "corporate_actions": ("ids",),
    "story_clusters": ("ids",),
    "market_data": ("symbols", "timeframes", "timestamps"),
    "positioning_reports": ("sources", "market_ids", "report_dates", "categories"),
    "option_chain_snapshots": ("sources", "symbols", "captured_ats"),
}


def _recovery_table(sql: str) -> str:
    match = re.search(r"autonomy_identity_recovery:(\w+)", sql)
    return match.group(1) if match else ""


def _recovery_key_text(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _recovery_as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _recovery_literal_value(name: str, value: Any, element: Any) -> Any:
    """Coerce ISO strings to temporal objects for typed literal rendering.

    Recovery binds reverse-parsed temporal keys as ISO string lists (the
    server casts them); literal-bind rendering needs real temporal values.
    """
    date_like = isinstance(element, postgresql.DATE) or element is postgresql.DATE
    stamp_like = (
        isinstance(element, postgresql.TIMESTAMP) or element is postgresql.TIMESTAMP
    )
    if not (date_like or stamp_like):
        return value
    values = value if isinstance(value, list) else [value]
    coerced = []
    for item in values:
        if isinstance(item, str):
            if date_like:
                coerced.append(date.fromisoformat(item[:10]))
            else:
                coerced.append(datetime.fromisoformat(item.replace("Z", "+00:00")))
        else:
            coerced.append(item)
    return coerced if isinstance(value, list) else coerced[0]


class _SessionSavepoint:
    """Savepoint context manager with real rollback semantics.

    Models a real nested transaction: an exception escaping the block
    rolls the savepoint back (the surrounding transaction stays usable)
    and re-raises, and a later ``begin_nested`` starts a fresh savepoint.
    The session records the lifecycle so tests can assert that recovery
    really rolled back instead of swallowing the failure.
    """

    def __init__(self, session: "MemorySession"):
        self.session = session

    def __enter__(self):
        self.session.savepoints += 1
        return self.session

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.session.savepoint_rollbacks += 1
        return False


def _bar_available(rest: tuple, timestamp: datetime) -> datetime:
    """Row revision time for one seeded market bar tuple.

    Mirrors the SQL ``COALESCE(updated_at, created_at)`` availability
    bound: an explicit ``updated_at`` (index 1 of ``rest``) wins, then
    ``created_at`` (index 0), then the event timestamp itself for bare
    ``(timestamp, close)`` seeds that predate the cutoffs.
    """
    if len(rest) > 1:
        return rest[1]
    if rest:
        return rest[0]
    return timestamp


class MemorySession:
    """Content-routing in-memory fake covering the cycle's SQL surface."""

    def __init__(self):
        self.calls = []
        # Identity keys of every catalyst_identity_lock advisory-lock call,
        # recorded separately from the fusion canonical-key lock so tests
        # can tell the two transaction-scoped locks apart.
        self.catalyst_lock_keys: list[str] = []
        # Identity keys of every risk_identity_lock advisory-lock call.
        self.risk_lock_keys: list[str] = []
        self.themes: dict[str, str] = {}
        self.theses: dict[str, dict] = {}
        self.versions: dict[str, int] = {}
        self.evidence: list[dict] = []
        self.catalysts: list[dict] = []
        self.scenarios: list[dict] = []
        self.risks: list[dict] = []
        self.groups: dict[str, dict] = {}
        self.members: list[dict] = []
        self.snapshots: set[tuple[str, str]] = set()
        self.falsification_runs: dict[tuple[str, str], dict] = {}
        self.forecasts: list[dict] = []
        self.outcomes: dict[str, dict] = {}
        self.market_data: dict[str, list[tuple[datetime, float]]] = {}
        self.position_links: list[dict] = []
        self.playbooks: list[dict] = []
        self.portfolio_holdings: dict[str, dict] = {}
        self.market_events: set[str] = set()
        self.event_matches: set[tuple[str, str, str]] = set()
        # Reference visibility of each match row: (playbook, event, kind) ->
        # {"observed_at": datetime, "created_at": datetime}.  Mirrors the
        # production NOT NULL columns so the second-pass reconstruction can
        # fail closed on matches that cannot be proven visible at a cutoff.
        self.event_match_times: dict[tuple[str, str, str], dict] = {}
        # Exact-ID recovery stores, keyed like the production source tables.
        self.recovery_source_documents: dict[str, dict] = {}
        self.recovery_research_source_claims: dict[str, dict] = {}
        self.recovery_filing_deltas: dict[str, dict] = {}
        self.recovery_observations: dict[str, dict] = {}
        self.recovery_analyses: dict[str, dict] = {}
        self.recovery_market_data: dict[tuple[str, str, datetime], dict] = {}
        self.recovery_corporate_actions: dict[str, dict] = {}
        self.recovery_positioning: dict[tuple[str, str, str, str], dict] = {}
        self.recovery_option_snapshots: dict[tuple[str, str, datetime], dict] = {}
        self.recovery_story_clusters: dict[str, dict] = {}
        # Nested-transaction lifecycle: savepoint entries and rollbacks,
        # plus the set of physical recovery tables whose query is
        # simulated to fail (failures surface inside a savepoint).
        self.savepoints = 0
        self.savepoint_rollbacks = 0
        self.failing_recovery_tables: set[str] = set()

    # -- helpers -----------------------------------------------------------

    def seed_thesis(self, thesis_id: str, **overrides) -> dict:
        row = {
            "id": thesis_id,
            "claim": "Existing claim",
            "variant_perception": None,
            "confidence": None,
            "status": "active",
            "trend_context": None,
            "valuation_context": None,
            "sentiment_context": None,
            "citation_map": {},
            "canonical_key": f"key:{thesis_id}",
            "input_fingerprint": None,
            "company": None,
            "symbol": None,
            "direction": "long",
            "horizon": "months",
            "mechanism": "existing mechanism",
            "catalyst_summary": None,
            "invalidation_conditions": [],
            "opportunity_score": 0.0,
            "last_evaluated_at": None,
            # Mirrors the nullable migration 055 accepted-reference guard
            # pair: absent for manual/legacy content, set together by
            # autonomous claims.
            "fusion_reference_at": None,
            "fusion_candidate_fingerprint": None,
            # Mirrors the production DEFAULT NOW() columns: the second-pass
            # reconstruction fails closed on missing/absent timestamps.
            "created_at": NOW - timedelta(days=1),
            "updated_at": NOW - timedelta(days=1),
        }
        row.update(overrides)
        self.theses[thesis_id] = row
        return row

    def seed_evidence(self, thesis_id: str, **overrides) -> dict:
        row = {
            "thesis_id": thesis_id,
            "evidence_type": "source_claim",
            "evidence_id": "claim:seed",
            "relationship": "supports",
            "excerpt": None,
            "source_family": "filings",
            "origin_key": None,
            "independence_key": None,
            "evidence_fingerprint": "c" * 64,
            "source_timestamp": NOW - timedelta(days=2),
            "available_at": NOW - timedelta(days=2),
            "quality_score": 0.5,
            "entailment_score": 0.5,
            "freshness_score": 0.5,
            "effective_weight": 1.0,
            "created_at": NOW - timedelta(days=2),
        }
        row.update(overrides)
        self.evidence.append(row)
        return row

    def seed_forecast(self, forecast_id: str, **overrides) -> dict:
        row = {
            "id": forecast_id,
            "thesis_id": EXISTING_ID,
            "scenario_id": None,
            "forecast_key": f"key:{forecast_id}",
            "forecast_type": "price",
            "direction": "up",
            "target_value": 105.0,
            "target_date": NOW.date() - timedelta(days=1),
            "as_of": NOW - timedelta(days=100),
            "version": 1,
            "superseded_at": None,
            "created_at": NOW - timedelta(days=100),
        }
        row.update(overrides)
        self.forecasts.append(row)
        return row

    def seed_holding(self, position_id: str, symbol: str) -> dict:
        holding = {"id": position_id, "symbol": symbol}
        self.portfolio_holdings[position_id] = holding
        return holding

    def seed_playbook(self, thesis_id: str, **overrides) -> dict:
        row = {
            "id": _id(f"playbook-seed:{thesis_id}"),
            "thesis_id": thesis_id,
            "playbook_key": f"key:{thesis_id}",
            "version": 1,
            "input_fingerprint": "f" * 64,
            "superseded_at": None,
            "expected_at": None,
            "event_types": ["headline_published", "story_updated"],
            "cited_evidence_refs": ["source_claim:claim:0000"],
            # Mirrors the production DEFAULT NOW() column; the second-pass
            # reconstruction only counts playbooks visible at the reference.
            "created_at": NOW - timedelta(days=1),
        }
        row.update(overrides)
        self.playbooks.append(row)
        return row

    def seed_context_match(
        self,
        playbook_id: str,
        event_id: str,
        *,
        observed_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> None:
        key = (playbook_id, str(event_id), "context")
        self.event_matches.add(key)
        # Reference-visible matches: the second-pass reconstruction counts a
        # match only when both rows are provably visible at the cutoff.
        self.event_match_times[key] = {
            "observed_at": observed_at or (NOW - timedelta(days=2)),
            "created_at": created_at or (NOW - timedelta(days=2)),
        }

    def _match_visible_at(
        self,
        key: tuple[str, str, str],
        *,
        reference: datetime | None,
        context_since: datetime | None,
    ) -> bool:
        """True when a context match row is provably visible at the cutoff.

        Fail closed: matches without recorded observed/created timestamps
        are never visible, and matches observed outside the recent window
        (``context_since``..``reference``) do not count as context.
        """
        if reference is None:
            return True
        times = self.event_match_times.get(key)
        if times is None:
            return False
        observed = times.get("observed_at")
        created = times.get("created_at")
        return (
            observed is not None
            and created is not None
            and created <= reference
            and observed <= reference
            and (context_since is None or observed >= context_since)
        )

    def _thesis_visible_at(
        self,
        thesis: dict,
        *,
        reference: datetime | None,
    ) -> bool:
        """True when a thesis row is provably visible at a maintenance
        backfill cutoff, mirroring the production reference guards.

        Fail closed: created/updated must be present and at/before the
        reference (a NULL timestamp can never be proven visible, exactly
        like the production ``<= :reference`` NULL comparison), and an
        accepted fusion reference may be absent or at/before it.  Without
        a reference bound the legacy no-cutoff behavior applies.
        """
        if reference is None:
            return True
        created = thesis.get("created_at")
        updated = thesis.get("updated_at")
        if created is None or updated is None:
            return False
        if created > reference:
            return False
        if updated > reference:
            return False
        fusion_reference = thesis.get("fusion_reference_at")
        return fusion_reference is None or fusion_reference <= reference

    # -- dispatch ----------------------------------------------------------

    def _recovery_rows(self, sql: str, params: dict) -> list[dict]:
        """Serve exact-ID recovery rows from the seeded source-record stores."""
        table = _recovery_table(sql)
        store = getattr(self, _RECOVERY_STORE_TABLES.get(table, ""), {})
        id_params = _RECOVERY_ID_PARAMS.get(table, ())
        available_by = params.get("available_by")
        rows = []
        for key, row in store.items():
            key_tuple = key if isinstance(key, tuple) else (key,)
            if len(key_tuple) != len(id_params):
                continue
            if not all(
                _recovery_key_text(part)
                in [_recovery_key_text(value) for value in params.get(name, [])]
                for part, name in zip(key_tuple, id_params, strict=False)
            ):
                continue
            if available_by is not None and row.get("created_at") is not None:
                if _recovery_as_datetime(row["created_at"]) > _recovery_as_datetime(
                    available_by
                ):
                    continue
            rows.append(dict(row))
        return rows

    def begin_nested(self):
        """Nested-transaction context; see :class:`_SessionSavepoint`."""
        return _SessionSavepoint(self)

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.calls.append((sql, params if params is not None else {}))
        many = isinstance(params, list)
        entries = params if many else [params or {}]
        if "pg_advisory_xact_lock" in sql:
            # Transaction-scoped serialization: routed, no rows.  The
            # catalyst identity lock is namespaced apart from the fusion
            # canonical-key lock (distinct ``lock_key`` values carrying
            # the catalyst_identity: prefix), so the fake records the two
            # locks separately; risk identity locks are recorded the same
            # way under their own prefix.
            for entry in entries:
                lock_key = entry.get("lock_key") or entry.get("key")
                if isinstance(lock_key, str) and lock_key.startswith(
                    "catalyst_identity:"
                ):
                    self.catalyst_lock_keys.append(lock_key)
                elif isinstance(lock_key, str) and lock_key.startswith(
                    "risk_identity:"
                ):
                    self.risk_lock_keys.append(lock_key)
            return Result()
        if "autonomy_identity_recovery" in sql:
            table = _recovery_table(sql)
            if table in self.failing_recovery_tables:
                raise RuntimeError(f"simulated {table} query failure")
            return Result(rows=self._recovery_rows(sql, entries[0] if entries else {}))
        if "autonomy_identity_backfill" in sql:
            limit = int(entries[0]["limit"])
            reference = entries[0].get("reference")
            thesis_rows = [
                row
                for row in self.theses.values()
                if (not row.get("company") or not row.get("symbol"))
                and self._thesis_visible_at(row, reference=reference)
            ][:limit]
            rows = []
            for thesis in thesis_rows:
                linked = [
                    row for row in self.evidence if row["thesis_id"] == thesis["id"]
                ] or [{"evidence_id": None}]
                rows.extend(
                    {
                        "id": thesis["id"],
                        "claim": thesis["claim"],
                        "company": thesis.get("company"),
                        "symbol": thesis.get("symbol"),
                        "evidence_type": link.get("evidence_type"),
                        "evidence_id": link.get("evidence_id"),
                    }
                    for link in linked
                )
            return Result(rows=rows)
        if "autonomy_catalyst_backfill" in sql:
            limit = int(entries[0]["limit"])
            reference = entries[0].get("reference")
            rows = []
            for thesis in self.theses.values():
                if not self._thesis_visible_at(thesis, reference=reference):
                    continue
                description = thesis.get("catalyst_summary")
                if not description:
                    continue
                if any(
                    row["thesis_id"] == thesis["id"]
                    and row["description"] == description
                    for row in self.catalysts
                ):
                    continue
                rows.append(
                    {
                        "id": thesis["id"],
                        "catalyst_summary": description,
                    }
                )
            return Result(rows=rows[:limit])

        if sql.startswith("SELECT status, started_at") and "falsification_runs" in sql:
            run_id = entries[0]["id"]
            for run in self.falsification_runs.values():
                if run["id"] == str(run_id):
                    return Result(
                        first={
                            "status": run["status"],
                            "started_at": run["started_at"],
                            "completed_at": run["completed_at"],
                        }
                    )
            return Result()
        if sql.startswith("SELECT status FROM") and "falsification_runs" in sql:
            run_id = entries[0]["id"]
            for run in self.falsification_runs.values():
                if run["id"] == str(run_id):
                    return Result(first={"status": run["status"]})
            return Result()
        if (
            "FROM investment_thesis_falsification_runs" in sql
            and "WHERE thesis_id" in sql
        ):
            thesis_id = str(entries[0]["thesis_id"])
            run_key = str(entries[0]["run_key"])
            run = self.falsification_runs.get((thesis_id, run_key))
            return Result(first={"id": run["id"]} if run else None)
        if sql.startswith("INSERT INTO investment_thesis_falsification_runs"):
            thesis_id = str(entries[0]["thesis_id"])
            run_key = str(entries[0]["run_key"])
            run_id = _id(f"run:{thesis_id}:{run_key}")
            self.falsification_runs[(thesis_id, run_key)] = {
                "id": run_id,
                "status": entries[0]["status"],
                "started_at": entries[0]["started_at"],
                "completed_at": None,
                "findings": entries[0]["findings"],
            }
            return Result(first={"id": run_id})
        if sql.startswith("UPDATE investment_thesis_falsification_runs"):
            run_id = str(entries[0]["id"])
            for run in self.falsification_runs.values():
                if run["id"] == run_id:
                    run["status"] = entries[0]["status"]
                    run["findings"] = entries[0]["findings"]
                    run["completed_at"] = entries[0]["completed_at"]
            return Result(rowcount=1)

        if "autonomy_forecast_backfill" in sql:
            reference = entries[0]["reference"]
            rows = []
            for scenario in self.scenarios:
                thesis = self.theses.get(str(scenario["thesis_id"]))
                if (
                    thesis is None
                    or thesis["status"] not in ("active", "candidate")
                    or not thesis.get("symbol")
                ):
                    continue
                # Reference-visible thesis state, mirroring the production
                # WHERE: created/last updated at/before the cutoff and no
                # accepted fusion reference later than it (NULL for legacy
                # content that never carried a guard).
                if (thesis.get("created_at") or NOW) > reference:
                    continue
                if (thesis.get("updated_at") or NOW) > reference:
                    continue
                fusion_reference = thesis.get("fusion_reference_at")
                if fusion_reference is not None and fusion_reference > reference:
                    continue
                # Reference-visible scenario lifecycle: created at/before
                # the cutoff and not superseded on/before it.  Missing
                # created_at stands for the NOT NULL DEFAULT NOW() insert
                # timestamp, exactly like the scenario-read handler.
                if (scenario.get("created_at") or NOW) > reference:
                    continue
                if (
                    scenario["superseded_at"] is not None
                    and scenario["superseded_at"] <= reference
                ):
                    continue
                if any(
                    row["scenario_id"] == scenario["id"]
                    and row["superseded_at"] is None
                    for row in self.forecasts
                ):
                    continue
                # A forecast that was active AT the reference also excludes
                # the scenario: the row was frozen (as_of) at/before the
                # reference and not superseded on/before it, even if it was
                # later superseded or moved to another scenario.
                if any(
                    row["scenario_id"] == scenario["id"]
                    and (row.get("as_of") or NOW) <= reference
                    and (
                        row["superseded_at"] is None or row["superseded_at"] > reference
                    )
                    for row in self.forecasts
                ):
                    continue
                rows.append(
                    {
                        "thesis_id": thesis["id"],
                        "symbol": thesis["symbol"],
                        "direction": thesis["direction"],
                        "horizon": thesis["horizon"],
                        "input_fingerprint": thesis.get("input_fingerprint"),
                        "scenario_id": scenario["id"],
                        "name": scenario["name"],
                        "expected_return": scenario["expected_return"],
                    }
                )
            return Result(rows=rows[: int(entries[0]["limit"])])

        if (
            "FROM investment_forecast_outcomes" in sql
            and "FROM investment_thesis_forecasts f" not in sql
        ):
            forecast_id = str(entries[0].get("id") or entries[0].get("forecast_id"))
            return Result(
                first={"present": True} if forecast_id in self.outcomes else None
            )
        if "FROM investment_thesis_forecasts f" in sql:
            reference = entries[0]["reference"]
            as_of_date = entries[0]["as_of_date"]
            rows = [
                {
                    **dict(row),
                    "symbol": self.theses.get(str(row["thesis_id"]), {}).get("symbol"),
                }
                for row in self.forecasts
                if (row["superseded_at"] is None or row["superseded_at"] > reference)
                and row["as_of"] <= reference
                and (row.get("created_at") or row["as_of"]) <= reference
                and row["forecast_type"] == "price"
                and row["target_date"] is not None
                and row["target_date"] < as_of_date
                and str(row["id"]) not in self.outcomes
            ]
            rows.sort(key=lambda row: (row["target_date"], row["id"]))
            return Result(rows=rows)
        if "FROM investment_thesis_forecasts WHERE forecast_key" in sql:
            key = str(entries[0]["key"])
            for row in self.forecasts:
                if row["forecast_key"] == key and row["superseded_at"] is None:
                    return Result(first=dict(row))
            return Result()
        if sql.startswith("INSERT INTO investment_thesis_forecasts"):
            key = str(entries[0]["forecast_key"])
            existing = [
                row
                for row in self.forecasts
                if row["forecast_key"] == key and row["superseded_at"] is None
            ]
            version = 1
            if existing:
                existing[0]["superseded_at"] = NOW
                version = int(existing[0]["version"]) + 1
            row = {
                "id": _id(f"forecast:{key}"),
                "thesis_id": str(entries[0]["thesis_id"]),
                "scenario_id": entries[0]["scenario_id"],
                "forecast_key": key,
                "forecast_type": entries[0]["forecast_type"],
                "direction": entries[0]["direction"],
                "target_value": entries[0]["target_value"],
                "target_date": entries[0]["target_date"],
                "as_of": entries[0]["as_of"],
                "version": version,
                "superseded_at": None,
                "created_at": NOW,
            }
            self.forecasts.append(row)
            return Result(first={"id": row["id"]})
        if sql.startswith("INSERT INTO investment_forecast_outcomes"):
            forecast_id = str(entries[0]["forecast_id"])
            self.outcomes[forecast_id] = {
                "status": entries[0]["status"],
                "actual_value": entries[0].get("actual_value"),
                "measured_at": entries[0].get("measured_at"),
            }
            return Result(first={"id": _id(f"outcome:{forecast_id}")}, rowcount=1)
        if (
            "SELECT 1 AS present FROM investment_thesis_forecasts" in sql
            and "scenario_id" in sql
        ):
            scenario_id = str(entries[0]["scenario_id"])
            return Result(
                first={"present": True}
                if any(
                    row["scenario_id"] == scenario_id and row["superseded_at"] is None
                    for row in self.forecasts
                )
                else None
            )
        if "SELECT 1 AS present FROM investment_thesis_forecasts" in sql:
            return Result(first={"present": True})
        if (
            "FROM investment_thesis_forecasts" in sql
            and "scenario_id = CAST(:scenario_id AS UUID)" in sql
            and "forecast_key" in sql
        ):
            # freeze_forecast scenario preflight: the active forecast on a
            # scenario is authoritative; the caller reports it (loser
            # contract) when it belongs to a different forecast_key.
            scenario_id = str(entries[0]["scenario_id"])
            for row in self.forecasts:
                if row["scenario_id"] == scenario_id and row["superseded_at"] is None:
                    return Result(first=dict(row))
            return Result()

        if "FROM investment_opportunity_snapshots" in sql:
            thesis_id = str(entries[0]["thesis_id"])
            key = str(entries[0]["snapshot_key"])
            return Result(
                first={"present": True} if (thesis_id, key) in self.snapshots else None
            )
        if sql.startswith("INSERT INTO investment_opportunity_snapshots"):
            thesis_id = str(entries[0]["thesis_id"])
            key = str(entries[0]["snapshot_key"])
            self.snapshots.add((thesis_id, key))
            return Result(rowcount=1)

        if sql.startswith("INSERT INTO investment_catalysts"):
            thesis_id = str(entries[0]["thesis_id"])
            description = str(entries[0]["description"])
            if any(
                row["thesis_id"] == thesis_id and row["description"] == description
                for row in self.catalysts
            ):
                return Result()
            row = {
                "id": _id(f"catalyst:{thesis_id}:{description}"),
                "thesis_id": thesis_id,
                "description": description,
                "expected_at": None,
                "state": "pending",
                "created_at": NOW,
            }
            self.catalysts.append(row)
            return Result(first={"id": row["id"]})
        if "FROM investment_catalysts" in sql:
            # Replay-safe evaluate: only catalysts created at/before the
            # reference are visible; rows without a stored timestamp are
            # treated as created now.
            thesis_id = str(entries[0]["id"])
            as_of = entries[0].get("as_of") or NOW
            return Result(
                rows=[
                    {
                        "description": row["description"],
                        "expected_at": row["expected_at"],
                        "state": row["state"],
                    }
                    for row in self.catalysts
                    if row["thesis_id"] == thesis_id
                    and (row.get("created_at") or NOW) <= as_of
                ]
            )

        if (
            "FROM investment_thesis_evidence" in sql
            and "evidence_fingerprint IS NOT NULL" in sql
        ):
            thesis_id = str(entries[0]["id"])
            rows = [
                {
                    "evidence_fingerprint": row["evidence_fingerprint"],
                    "independence_key": row["independence_key"],
                }
                for row in self.evidence
                if row["thesis_id"] == thesis_id
            ]
            return Result(rows=rows)
        if "FROM investment_thesis_evidence" in sql:
            # Replay-safe evaluate: only evidence links persisted (created_at)
            # at/before the cutoff whose effective source and availability
            # timestamps also predate it are visible; evaluate passes as_of,
            # the second-pass snapshot loader passes reference.  A link
            # attached after the cutoff is invisible to persisted queries and
            # enters scoring only as an explicit current-cycle input.
            thesis_id = str(entries[0]["id"])
            as_of = entries[0].get("as_of") or entries[0].get("reference") or NOW
            rows = [
                {
                    key: row.get(key)
                    for key in (
                        "evidence_type",
                        "evidence_id",
                        "relationship",
                        "excerpt",
                        "source_family",
                        "origin_key",
                        "independence_key",
                        "evidence_fingerprint",
                        "source_timestamp",
                        "available_at",
                        "quality_score",
                        "entailment_score",
                        "freshness_score",
                        "effective_weight",
                        "created_at",
                    )
                }
                for row in self.evidence
                if row["thesis_id"] == thesis_id
                and (row.get("created_at") or NOW) <= as_of
                and (row.get("source_timestamp") or row.get("created_at") or NOW)
                <= as_of
                and (
                    row.get("available_at")
                    or row.get("source_timestamp")
                    or row.get("created_at")
                    or NOW
                )
                <= as_of
            ]
            return Result(rows=rows)
        if sql.startswith("INSERT INTO investment_thesis_evidence"):
            inserted = 0
            for entry in entries:
                thesis_id = str(entry["thesis_id"])
                fingerprint = str(entry["evidence_fingerprint"])
                if any(
                    row["thesis_id"] == thesis_id
                    and row["evidence_fingerprint"] == fingerprint
                    for row in self.evidence
                ):
                    continue
                independence = entry.get("independence_key")
                if independence and any(
                    row["thesis_id"] == thesis_id
                    and row.get("independence_key") == independence
                    for row in self.evidence
                ):
                    continue
                row = dict(entry)
                row.setdefault("created_at", NOW)
                self.evidence.append(row)
                inserted += 1
            return Result(rowcount=inserted)
        if sql.startswith("UPDATE investment_theses SET last_evidence_at"):
            return Result(rowcount=1)

        if "FROM investment_thesis_scenarios" in sql and "AND name = :name" in sql:
            thesis_id = str(entries[0]["thesis_id"])
            name = str(entries[0]["name"])
            for row in self.scenarios:
                if (
                    row["thesis_id"] == thesis_id
                    and row["name"] == name
                    and row["superseded_at"] is None
                ):
                    return Result(first=dict(row))
            return Result()
        if (
            "FROM investment_thesis_scenarios" in sql
            and "id = CAST(:id AS UUID)" in sql
            and "FOR UPDATE" in sql
        ):
            # freeze_forecast scenario-row lock: existence was already
            # validated and the returned row is discarded, so serve the
            # matching row (or an empty no-op) without the base-case
            # semantics of the generic scenario lock below.
            scenario_id = str(entries[0]["id"])
            for row in self.scenarios:
                if row["id"] == scenario_id:
                    return Result(first=dict(row))
            return Result()
        if "FROM investment_thesis_scenarios" in sql and "FOR UPDATE" in sql:
            thesis_id = str(entries[0]["thesis_id"])
            for row in self.scenarios:
                if (
                    row["thesis_id"] == thesis_id
                    and row.get("is_base_case")
                    and row["superseded_at"] is None
                ):
                    return Result(first=dict(row))
            return Result()
        if "SELECT 1 AS present FROM investment_thesis_scenarios" in sql:
            return Result(first={"present": True})
        if "FROM investment_thesis_scenarios" in sql:
            # Replay-safe evaluate: scenario versions created at/before the
            # reference and not superseded before it; rows without a stored
            # timestamp are treated as created now.
            thesis_id = str(entries[0]["id"])
            as_of = entries[0].get("as_of") or entries[0].get("reference") or NOW
            rows = [
                {
                    "name": row["name"],
                    "probability": row["probability"],
                    "expected_return": row["expected_return"],
                }
                for row in self.scenarios
                if row["thesis_id"] == thesis_id
                and (row.get("created_at") or NOW) <= as_of
                and (row["superseded_at"] is None or row["superseded_at"] > as_of)
            ]
            rows.sort(key=lambda row: (row["name"],))
            return Result(rows=rows)
        if sql.startswith("UPDATE investment_thesis_scenarios SET superseded_at"):
            # Immutable-version supersede: production stamps the active row
            # (one-time NULL -> NOW transition, guarded by superseded_at IS
            # NULL) before inserting the next version of a changed leg.
            scenario_id = str(entries[0]["id"])
            for row in self.scenarios:
                if row["id"] == scenario_id and row["superseded_at"] is None:
                    row["superseded_at"] = NOW
                    return Result(rowcount=1)
            return Result(rowcount=0)
        if sql.startswith("INSERT INTO investment_thesis_scenarios"):
            thesis_id = str(entries[0]["thesis_id"])
            name = str(entries[0]["name"])
            active = [
                row
                for row in self.scenarios
                if row["thesis_id"] == thesis_id
                and row["name"] == name
                and row["superseded_at"] is None
            ]
            for row in active:
                row["superseded_at"] = NOW
            # Production computes and passes the next version (active
            # version + 1, or 1); the supersede above already stamped the
            # active row, so the store can no longer derive it.
            version = int(entries[0]["version"])
            row = {
                "id": _id(f"scenario:{thesis_id}:{name}"),
                "thesis_id": thesis_id,
                "name": name,
                "description": entries[0]["description"],
                "probability": entries[0]["probability"],
                "expected_return": entries[0]["expected_return"],
                "is_base_case": bool(entries[0]["is_base_case"]),
                "version": version,
                "superseded_at": None,
                "created_at": NOW,
            }
            self.scenarios.append(row)
            return Result(first={"id": row["id"]})

        if sql.startswith("INSERT INTO investment_risks"):
            thesis_id = str(entries[0]["thesis_id"])
            description = str(entries[0]["description"])
            existing = next(
                (
                    row
                    for row in self.risks
                    if row["thesis_id"] == thesis_id
                    and row["description"] == description
                ),
                None,
            )
            if existing is not None:
                # Idempotent rerun: the NOT EXISTS guard plus the identity
                # lock already closed the race in production.
                return Result()
            row = {
                "id": _id(f"risk:{thesis_id}:{description}"),
                "thesis_id": thesis_id,
                "description": description,
                "kind": str(entries[0]["kind"]),
                "severity": str(entries[0]["severity"]),
                "created_at": NOW,
                "updated_at": NOW,
            }
            self.risks.append(row)
            return Result(first={"id": row["id"]})

        if "FROM investment_thesis_group_members" in sql:
            group_id = str(entries[0]["group_id"])
            thesis_id = str(entries[0]["thesis_id"])
            for row in self.members:
                if (
                    row["group_id"] == group_id
                    and row["thesis_id"] == thesis_id
                    and row["removed_at"] is None
                ):
                    return Result(first={"present": True})
            return Result()
        if sql.startswith("INSERT INTO investment_thesis_group_members"):
            group_id = str(entries[0]["group_id"])
            thesis_id = str(entries[0]["thesis_id"])
            self.members.append(
                {
                    "group_id": group_id,
                    "thesis_id": thesis_id,
                    "removed_at": None,
                }
            )
            return Result(rowcount=1)
        if sql.startswith("INSERT INTO investment_thesis_groups"):
            name = str(entries[0]["name"])
            if name in self.groups:
                return Result()
            self.groups[name] = {
                "id": _id(f"group:{name}"),
                "name": name,
                "description": entries[0]["description"],
                "status": "active",
            }
            return Result(first={"id": self.groups[name]["id"]})
        if "FROM investment_thesis_groups WHERE name" in sql:
            name = str(entries[0]["name"])
            group = self.groups.get(name)
            return Result(first={"id": group["id"]} if group else None)
        if "SELECT 1 AS present FROM investment_thesis_groups" in sql:
            return Result(first={"present": True})

        if "FROM investment_thesis_versions" in sql:
            thesis_id = str(entries[0]["id"])
            return Result(first={"max_version": self.versions.get(thesis_id, 0)})
        if sql.startswith("INSERT INTO investment_thesis_versions"):
            thesis_id = str(entries[0]["thesis_id"])
            self.versions[thesis_id] = self.versions.get(thesis_id, 0) + 1
            return Result(rowcount=1)

        if "FROM investment_theses WHERE input_fingerprint" in sql:
            fingerprint = str(entries[0]["fingerprint"])
            for row in self.theses.values():
                if row.get("input_fingerprint") == fingerprint:
                    return Result(
                        first={
                            "id": row["id"],
                            "claim": row["claim"],
                            "variant_perception": row["variant_perception"],
                            "confidence": row["confidence"],
                            "status": row["status"],
                            "trend_context": row.get("trend_context"),
                            "valuation_context": row.get("valuation_context"),
                            "sentiment_context": row.get("sentiment_context"),
                            "citation_map": row.get("citation_map", {}),
                            "canonical_key": row["canonical_key"],
                            "fusion_reference_at": row.get("fusion_reference_at"),
                            "fusion_candidate_fingerprint": row.get(
                                "fusion_candidate_fingerprint"
                            ),
                        }
                    )
            return Result()
        if "FROM investment_theses" in sql and "FOR UPDATE" in sql:
            # Row-lock probes: the membership lock, the pause lock, and the
            # merge's accepted-reference claim.  The merge lookups select by
            # canonical_key/fingerprint (:key/:fingerprint) and need the
            # full row incl. the reference guard; the id-based locks only
            # need a present row, but the extra keys are harmless.
            params = entries[0]
            if "input_fingerprint" in sql:
                fingerprint = str(params.get("fingerprint") or "")
                row = next(
                    (
                        thesis
                        for thesis in self.theses.values()
                        if thesis.get("input_fingerprint") == fingerprint
                    ),
                    None,
                )
            elif "canonical_key" in sql:
                key = str(params.get("key") or "")
                row = next(
                    (
                        thesis
                        for thesis in self.theses.values()
                        if thesis.get("canonical_key") == key
                    ),
                    None,
                )
            else:
                row = self.theses.get(str(params.get("id") or ""))
            if row is None:
                return Result()
            return Result(
                first={
                    "id": row["id"],
                    "claim": row["claim"],
                    "variant_perception": row["variant_perception"],
                    "confidence": row["confidence"],
                    "status": row["status"],
                    "trend_context": row.get("trend_context"),
                    "valuation_context": row.get("valuation_context"),
                    "sentiment_context": row.get("sentiment_context"),
                    "citation_map": row.get("citation_map", {}),
                    "canonical_key": row.get("canonical_key"),
                    "fusion_reference_at": row.get("fusion_reference_at"),
                    "fusion_candidate_fingerprint": row.get(
                        "fusion_candidate_fingerprint"
                    ),
                }
            )
        if "FROM investment_theses WHERE canonical_key" in sql:
            key = str(entries[0]["key"])
            for row in self.theses.values():
                if row.get("canonical_key") == key:
                    return Result(
                        first={
                            "id": row["id"],
                            "claim": row["claim"],
                            "variant_perception": row["variant_perception"],
                            "confidence": row["confidence"],
                            "status": row["status"],
                            "canonical_key": row.get("canonical_key"),
                            "fusion_reference_at": row.get("fusion_reference_at"),
                            "fusion_candidate_fingerprint": row.get(
                                "fusion_candidate_fingerprint"
                            ),
                        }
                    )
            return Result()
        if (
            "catalyst_identity_guard" in sql
            and "FROM investment_theses" in sql
            and "WHERE id = CAST(:thesis_id AS UUID)" in sql
        ):
            # Guarded catalyst helper: the stored canonical key feeds the
            # fusion lock acquired before the catalyst identity lock.
            thesis = self.theses.get(str(entries[0]["thesis_id"]))
            return Result(
                first={"canonical_key": thesis["canonical_key"]}
                if thesis is not None
                else None
            )
        if sql.startswith("SELECT COUNT(*)") and "FROM investment_theses" in sql:
            # Fail-closed diagnostic: active/candidate theses whose state is
            # not provably visible at the reference (missing or post-reference
            # created/updated timestamps, or post-reference scoring/fusion
            # state) are excluded from the second pass.
            reference = entries[0]["reference"]
            count = sum(
                1
                for row in self.theses.values()
                if row["status"] in ("active", "candidate")
                and (
                    row.get("created_at") is None
                    or row.get("updated_at") is None
                    or row["created_at"] > reference
                    or row["updated_at"] > reference
                    or (
                        row.get("last_evaluated_at") is not None
                        and row["last_evaluated_at"] > reference
                    )
                    or (
                        row.get("fusion_reference_at") is not None
                        and row["fusion_reference_at"] > reference
                    )
                )
            )
            return Result(first={"count": count})
        if "FROM investment_theses t WHERE t.status IN" in sql:
            reference = entries[0].get("reference")
            context_since = entries[0].get("context_since")
            rows = []
            for row in self.theses.values():
                if row["status"] not in ("active", "candidate"):
                    continue
                # Fail closed: a thesis whose state is not provably visible
                # at the reference is never a second-pass candidate.  Current
                # scoring/fusion state (last_evaluated_at, fusion_reference_at)
                # is reference-bounded too, so newer opportunity scores can
                # never steer an older run.
                if reference is not None:
                    created = row.get("created_at")
                    updated = row.get("updated_at")
                    last_evaluated = row.get("last_evaluated_at")
                    fusion_reference = row.get("fusion_reference_at")
                    if (
                        created is None
                        or updated is None
                        or created > reference
                        or updated > reference
                        or (last_evaluated is not None and last_evaluated > reference)
                        or (
                            fusion_reference is not None
                            and fusion_reference > reference
                        )
                    ):
                        continue
                has_link = any(
                    link["thesis_id"] == row["id"]
                    and (
                        reference is None
                        or (
                            link.get("created_at") is not None
                            and link["created_at"] <= reference
                        )
                    )
                    and (
                        link["removed_at"] is None
                        or (reference is not None and link["removed_at"] > reference)
                    )
                    for link in self.position_links
                )
                playbook_ids = [
                    playbook["id"]
                    for playbook in self.playbooks
                    if playbook["thesis_id"] == row["id"]
                    and (
                        reference is None
                        or (
                            playbook.get("created_at") is not None
                            and playbook["created_at"] <= reference
                            and (
                                playbook["superseded_at"] is None
                                or playbook["superseded_at"] > reference
                            )
                        )
                    )
                ]
                has_context = any(
                    (playbook_id, event_id, "context") in self.event_matches
                    and self._match_visible_at(
                        (playbook_id, event_id, "context"),
                        reference=reference,
                        context_since=context_since,
                    )
                    for playbook_id in playbook_ids
                    for event_id in {
                        event_id for (_, event_id, _kind) in self.event_matches
                    }
                )
                rows.append(
                    {
                        "id": row["id"],
                        "claim": row["claim"],
                        "direction": row["direction"],
                        "status": row["status"],
                        "invalidation_conditions": row["invalidation_conditions"],
                        "opportunity_score": row["opportunity_score"],
                        "last_evaluated_at": row["last_evaluated_at"],
                        "updated_at": row.get("updated_at"),
                        "fusion_reference_at": row.get("fusion_reference_at"),
                        "has_link": has_link,
                        "has_context": has_context,
                    }
                )
            rows.sort(
                key=lambda row: (
                    not row["has_link"],
                    not row["has_context"],
                    -(row["opportunity_score"] or 0.0),
                    -(
                        row["last_evaluated_at"].timestamp()
                        if row["last_evaluated_at"]
                        else 0
                    ),
                    row["id"],
                )
            )
            return Result(rows=rows)
        if sql.startswith("INSERT INTO investment_theses"):
            entry = entries[0]
            thesis_id = _id(f"thesis:{entry['canonical_key']}")
            self.theses[thesis_id] = {
                "id": thesis_id,
                "claim": entry["claim"],
                "variant_perception": entry["variant_perception"],
                "confidence": entry["confidence"],
                "trend_context": entry.get("trend_context"),
                "valuation_context": entry.get("valuation_context"),
                "sentiment_context": entry.get("sentiment_context"),
                "citation_map": json.loads(entry.get("citation_map") or "{}"),
                "status": "candidate",
                "canonical_key": entry["canonical_key"],
                "input_fingerprint": entry["input_fingerprint"],
                "company": entry.get("company"),
                "symbol": entry.get("symbol"),
                "direction": entry["direction"],
                "horizon": entry["horizon"],
                "mechanism": entry["mechanism"],
                "invalidation_conditions": entry["invalidation_conditions"],
                "opportunity_score": 0.0,
                "last_evaluated_at": None,
                # Mirrors the migration 055 accepted-reference guard pair:
                # the autonomous cycle persists reference and proven
                # candidate fingerprint together at creation.
                "fusion_reference_at": entry.get("fusion_reference_at"),
                "fusion_candidate_fingerprint": entry.get(
                    "fusion_candidate_fingerprint"
                ),
                # Mirrors the production DEFAULT NOW() lifecycle columns.
                "created_at": NOW,
                "updated_at": NOW,
            }
            return Result(first={"id": thesis_id})
        if sql.startswith("UPDATE investment_theses") and "BTRIM(company)" in sql:
            thesis_id = str(entries[0]["id"])
            row = self.theses.get(thesis_id)
            changed = False
            if row is not None:
                if not row.get("company") and entries[0].get("company"):
                    row["company"] = entries[0]["company"]
                    changed = True
                if not row.get("symbol") and entries[0].get("symbol"):
                    row["symbol"] = entries[0]["symbol"]
                    changed = True
                elif entries[0].get("normalize_symbol") and entries[0].get("symbol"):
                    stored = " ".join(str(row.get("symbol") or "").split()).upper()
                    resolved = " ".join(str(entries[0]["symbol"]).split()).upper()
                    if stored and stored == resolved:
                        row["symbol"] = entries[0]["symbol"]
                        changed = True
            return Result(rowcount=int(changed))
        if sql.startswith("UPDATE investment_theses SET group_id"):
            return Result(rowcount=1)
        if "SET status = 'paused'" in sql:
            thesis_id = str(entries[0]["id"])
            row = self.theses.get(thesis_id)
            if row is None:
                return Result(rowcount=0)
            if "updated_at" in entries[0]:
                # Optimistic conditional pause (second pass): the row must
                # still carry the exact status/updated_at tokens selected at
                # the reference, and last_evaluated_at/fusion_reference_at
                # must still equal the selected tokens or sit exactly at the
                # reference (a same-cycle recompute may set last_evaluated_at
                # to the reference; anything newer belongs to a
                # concurrent/newer cycle and blocks the pause).  Success
                # stamps updated_at = NOW() like the production UPDATE.
                if (
                    row["status"] != entries[0]["status"]
                    or row["updated_at"] != entries[0]["updated_at"]
                ):
                    return Result(rowcount=0)
                reference = entries[0]["reference"]
                last_evaluated = row.get("last_evaluated_at")
                if last_evaluated is not None and not (
                    last_evaluated == entries[0]["last_evaluated_at"]
                    or last_evaluated == reference
                ):
                    return Result(rowcount=0)
                fusion_reference = row.get("fusion_reference_at")
                if fusion_reference is not None and not (
                    fusion_reference == entries[0]["fusion_reference_at"]
                    or fusion_reference == reference
                ):
                    return Result(rowcount=0)
                row["status"] = "paused"
                row["updated_at"] = NOW
                return Result(rowcount=1)
            if row["status"] in ("active", "candidate"):
                row["status"] = "paused"
                return Result(rowcount=1)
            return Result(rowcount=0)
        if sql.startswith("UPDATE investment_theses SET claim"):
            # Merge content claim (autonomous path carries the accepted
            # reference and the proven candidate fingerprint in the same
            # UPDATE; the manual path carries neither).
            thesis_id = str(entries[0]["id"])
            row = self.theses.get(thesis_id)
            if row is not None:
                row["claim"] = entries[0]["claim"]
                row["variant_perception"] = entries[0]["variant_perception"]
                row["confidence"] = entries[0]["confidence"]
                row["trend_context"] = entries[0].get("trend_context")
                row["valuation_context"] = entries[0].get("valuation_context")
                row["sentiment_context"] = entries[0].get("sentiment_context")
                row["citation_map"] = json.loads(entries[0].get("citation_map") or "{}")
                if entries[0].get("accepted_reference") is not None:
                    row["fusion_reference_at"] = entries[0]["accepted_reference"]
                    row["fusion_candidate_fingerprint"] = entries[0][
                        "accepted_fingerprint"
                    ]
            return Result(rowcount=1)
        if sql.startswith("UPDATE investment_theses SET fusion_reference_at"):
            # Monotonic accepted-reference claim for an unchanged thesis:
            # advances the guard pair (IS DISTINCT FROM guard makes
            # identical re-claims no-ops, like the production WHERE).
            thesis_id = str(entries[0]["id"])
            row = self.theses.get(thesis_id)
            if row is not None:
                row["fusion_reference_at"] = entries[0]["accepted_reference"]
                row["fusion_candidate_fingerprint"] = entries[0]["accepted_fingerprint"]
            return Result(rowcount=1)
        if sql.startswith("UPDATE investment_theses SET evidence_strength"):
            thesis_id = str(entries[0]["id"])
            row = self.theses.get(thesis_id)
            if row is not None:
                as_of = entries[0].get("as_of") or NOW
                stored = row.get("last_evaluated_at")
                if stored is not None and stored > as_of:
                    # Monotonic persistence: an older finishing evaluation
                    # must not regress newer current ranking columns or
                    # last_evaluated_at (the SQL WHERE guard is a no-op).
                    return Result(rowcount=0)
                row["opportunity_score"] = float(entries[0]["opportunity_score"] or 0)
                row["last_evaluated_at"] = as_of
                row["expected_value"] = float(entries[0]["expected_value"] or 0)
                row["expected_shortfall"] = float(entries[0]["expected_shortfall"] or 0)
                row["catalyst_score"] = float(entries[0]["catalyst_score"] or 0)
                row["evidence_strength"] = float(entries[0]["evidence_strength"] or 0)
            return Result(rowcount=1)
        if "SELECT 1 AS present FROM investment_theses" in sql:
            thesis_id = str(entries[0]["id"])
            return Result(first={"present": True} if thesis_id in self.theses else None)

        if (
            "FROM investment_thesis_event_playbooks AS p" in sql
            and "JOIN investment_theses AS t" in sql
        ):
            rows = []
            for row in self.playbooks:
                if row["superseded_at"] is not None:
                    continue
                thesis = self.theses.get(row["thesis_id"], {})
                entity_keys = [
                    value
                    for value in (thesis.get("company"), thesis.get("symbol"))
                    if value
                ]
                rows.append(
                    {
                        "id": row["id"],
                        "thesis_id": row["thesis_id"],
                        "playbook_key": row["playbook_key"],
                        "version": row["version"],
                        "thesis_version": row.get("thesis_version", 1),
                        "catalyst": row.get("catalyst"),
                        "horizon": row.get("horizon", "months"),
                        "expected_at": row.get("expected_at"),
                        "event_types": row.get("event_types", []),
                        "trigger_conditions": row.get("trigger_conditions", []),
                        "confirmation_conditions": row.get(
                            "confirmation_conditions", []
                        ),
                        "invalidation_conditions": row.get(
                            "invalidation_conditions", []
                        ),
                        "bull_scenario": row.get("bull_scenario"),
                        "base_scenario": row.get("base_scenario"),
                        "bear_scenario": row.get("bear_scenario"),
                        "cited_evidence_refs": row.get("cited_evidence_refs", []),
                        "input_fingerprint": row.get("input_fingerprint"),
                        "superseded_at": row.get("superseded_at"),
                        "created_at": row.get("created_at", NOW),
                        "entity_keys": entity_keys,
                    }
                )
            rows.sort(
                key=lambda row: (
                    row["expected_at"] is not None,
                    row["created_at"],
                    row["id"],
                )
            )
            return Result(rows=rows)
        if "SELECT 1 AS present FROM investment_thesis_event_playbooks" in sql:
            playbook_id = str(entries[0]["id"])
            present = any(row["id"] == playbook_id for row in self.playbooks)
            return Result(first={"present": True} if present else None)
        if "FROM investment_thesis_event_playbooks" in sql:
            key = str(entries[0]["playbook_key"])
            for row in self.playbooks:
                if row["playbook_key"] == key and row["superseded_at"] is None:
                    return Result(
                        first={
                            "id": row["id"],
                            "thesis_id": row["thesis_id"],
                            "version": row["version"],
                            "input_fingerprint": row["input_fingerprint"],
                        }
                    )
            return Result()
        if sql.startswith("UPDATE investment_thesis_event_playbooks"):
            return Result(rowcount=1)
        if sql.startswith("INSERT INTO investment_thesis_event_playbooks"):
            key = str(entries[0]["playbook_key"])
            active = [
                row
                for row in self.playbooks
                if row["playbook_key"] == key and row["superseded_at"] is None
            ]
            version = 1
            if active:
                active[0]["superseded_at"] = NOW
                version = int(active[0]["version"]) + 1
            row = {
                "id": _id(f"playbook:{key}"),
                "thesis_id": str(entries[0]["thesis_id"]),
                "playbook_key": key,
                "version": version,
                "input_fingerprint": str(entries[0]["input_fingerprint"]),
                "superseded_at": None,
                "expected_at": entries[0]["expected_at"],
                "event_types": entries[0]["event_types"],
                "cited_evidence_refs": entries[0]["cited_evidence_refs"],
                "catalyst": entries[0]["catalyst"],
                "horizon": entries[0]["horizon"],
                "trigger_conditions": entries[0]["trigger_conditions"],
                "confirmation_conditions": entries[0]["confirmation_conditions"],
                "invalidation_conditions": entries[0]["invalidation_conditions"],
                "bull_scenario": entries[0]["bull_scenario"],
                "base_scenario": entries[0]["base_scenario"],
                "bear_scenario": entries[0]["bear_scenario"],
                "created_at": NOW,
            }
            self.playbooks.append(row)
            return Result(first={"id": row["id"], "version": row["version"]})

        if "FROM investment_thesis_event_matches" in sql:
            playbook_id = str(entries[0]["playbook_id"])
            event_id = str(entries[0]["market_event_id"])
            kind = str(entries[0]["match_kind"])
            return Result(
                first={"present": True}
                if (playbook_id, event_id, kind) in self.event_matches
                else None
            )
        if sql.startswith("INSERT INTO investment_thesis_event_matches"):
            playbook_id = str(entries[0]["playbook_id"])
            event_id = str(entries[0]["market_event_id"])
            kind = str(entries[0]["match_kind"])
            key = (playbook_id, event_id, kind)
            if key in self.event_matches:
                return Result(rowcount=0)
            self.event_matches.add(key)
            self.event_match_times[key] = {
                "observed_at": entries[0].get("observed_at")
                or (NOW - timedelta(days=2)),
                "created_at": entries[0].get("created_at") or NOW,
            }
            return Result(rowcount=1)
        if "SELECT 1 AS present FROM market_events" in sql:
            event_id = str(entries[0]["id"])
            return Result(
                first={"present": True} if event_id in self.market_events else None
            )

        if "FROM portfolio_holdings" in sql and "LOWER(TRIM(symbol))" in sql:
            symbol = str(entries[0]["symbol"])
            for holding in self.portfolio_holdings.values():
                if _normalized(holding["symbol"]) == symbol:
                    return Result(first={"id": holding["id"]})
            return Result()
        if "SELECT 1 AS present FROM portfolio_holdings" in sql:
            position_id = str(entries[0]["id"])
            return Result(
                first={"present": True}
                if position_id in self.portfolio_holdings
                else None
            )
        if "FROM position_thesis_links" in sql and "removed_at IS NULL LIMIT 1" in sql:
            position_id = str(entries[0]["position_id"])
            thesis_id = str(entries[0]["thesis_id"])
            link_type = str(entries[0]["link_type"])
            for link in self.position_links:
                if (
                    link["position_id"] == position_id
                    and link["thesis_id"] == thesis_id
                    and link["link_type"] == link_type
                    and link["removed_at"] is None
                ):
                    return Result(first={"present": True})
            return Result()
        if sql.startswith("INSERT INTO position_thesis_links"):
            position_id = str(entries[0]["position_id"])
            thesis_id = str(entries[0]["thesis_id"])
            link_type = str(entries[0]["link_type"])
            exists = any(
                link["position_id"] == position_id
                and link["thesis_id"] == thesis_id
                and link["link_type"] == link_type
                and link["removed_at"] is None
                for link in self.position_links
            )
            if not exists:
                self.position_links.append(
                    {
                        "position_id": position_id,
                        "thesis_id": thesis_id,
                        "link_type": link_type,
                        "created_at": NOW,
                        "removed_at": None,
                    }
                )
                return Result(
                    first={"id": _id(f"position-link:{position_id}:{thesis_id}")},
                    rowcount=1,
                )
            return Result(rowcount=0)

        if "JOIN market_data m" in sql:
            # Bounded liquidity lookback for evaluate_thesis: latest daily
            # close*volume bars per thesis symbol.  Seeded bars carry no
            # volume, so the fake supplies a fixed 1m-share turnover.
            thesis_id = str(entries[0]["id"])
            as_of = entries[0]["as_of"]
            limit = int(entries[0]["limit"])
            thesis = self.theses.get(thesis_id) or {}
            bars = sorted(
                (
                    (timestamp, close)
                    for timestamp, close, *rest in self.market_data.get(
                        thesis.get("symbol"), []
                    )
                    if timestamp <= as_of and _bar_available(rest, timestamp) <= as_of
                ),
                key=lambda item: item[0],
                reverse=True,
            )
            return Result(
                rows=[
                    {"close": close, "volume": 1_000_000.0} for _, close in bars[:limit]
                ]
            )

        if "FROM market_data" in sql:
            # Bars are (timestamp, close), (timestamp, close, created_at),
            # or (timestamp, close, created_at, updated_at); two-tuple
            # seeds are treated as already available and an explicit
            # updated_at (the row's last revision) wins over created_at,
            # mirroring COALESCE(updated_at, created_at) in the SQL.
            symbol = _canonical_market_symbol(entries[0]["symbol"])
            as_of = entries[0]["as_of"]
            available_at = entries[0].get("available_at") or as_of
            closes = [
                (timestamp, close)
                for timestamp, close, *rest in self.market_data.get(symbol, [])
                if timestamp <= as_of
                and timestamp >= entries[0].get("earliest", timestamp)
                and _bar_available(rest, timestamp) <= available_at
            ]
            if not closes:
                return Result()
            closes.sort(key=lambda item: item[0], reverse=True)
            return Result(first={"close": closes[0][1]})

        if "SELECT 1 AS present FROM investment_themes" in sql:
            return Result(first={"present": True})
        if "FROM investment_themes WHERE name" in sql:
            name = str(entries[0]["name"])
            theme_id = self.themes.get(name)
            return Result(first={"id": theme_id} if theme_id else None)
        if sql.startswith("INSERT INTO investment_themes"):
            name = str(entries[0]["name"])
            if name in self.themes:
                return Result()
            self.themes[name] = THEME_ID
            return Result(first={"id": THEME_ID})

        if "INSERT INTO generation_attempts" in sql:
            return Result(rowcount=1)

        raise AssertionError(f"unexpected SQL call: {sql}")


class RecordingSession:
    """Minimal recording session for the production-runner repair tests."""

    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params if params is not None else {}))
        return Result(rowcount=1)


class FakeStage:
    def __init__(self, factory):
        self.factory = factory
        self.policy = SimpleNamespace(model="test/model")

    def call(self, prompt):
        self.factory.prompts.append(prompt)
        response = self.factory.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return dict(response)


class FakeStageFactory:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.calls = 0

    def __call__(self, config, processor_id, **kwargs):
        self.calls += 1
        return FakeStage(self)


def llm_result(content: Any, cost_usd: float = 0.01) -> dict:
    return {
        "content": json.dumps(content) if not isinstance(content, str) else content,
        "cost_usd": cost_usd,
        "model": "test/model",
        "tokens_input": 10,
        "tokens_output": 5,
        "duration_ms": 7,
    }


def run_cycle(session, **overrides):
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
        return_value=EvidenceCollection(items=tuple(evidence_items()), failures={}),
    ):
        return run_autonomous_thesis_cycle(session, cycle_config(), **kwargs)


def attempt_rows(session: RecordingSession) -> list[dict]:
    rows = []
    for sql, params in session.calls:
        if "INSERT INTO generation_attempts" in sql:
            rows.append(params)
    return rows


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
        import yaml

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


class ProductionRunnerRepairTests(unittest.TestCase):
    def _runner(self, session, budget=1.0):
        return LLMRoleRunner(
            {}, correlation_id="corr-1", session=session, budget_cap_usd=budget
        )

    def test_first_valid_response_uses_one_call_and_records_one_attempt(self):
        session = RecordingSession()
        factory = FakeStageFactory([llm_result([CANDIDATE])])
        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session)
            output = runner.run(role="fundamental", prompt="prompt", schema={})
        self.assertIsInstance(output, list)
        self.assertEqual(factory.calls, 1)
        rows = attempt_rows(session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "validated")
        self.assertEqual(rows[0]["attempt_number"], 1)
        self.assertEqual(rows[0]["processor"], "thesis_autonomy")
        self.assertEqual(rows[0]["stage"], "fundamental")
        self.assertEqual(rows[0]["validation_issues"], "[]")
        self.assertEqual(rows[0]["correlation_id"], "corr-1")
        self.assertEqual(runner.calls, 1)
        self.assertEqual(runner.cost_usd, 0.01)

    def test_malformed_then_valid_repairs_exactly_once(self):
        session = RecordingSession()
        factory = FakeStageFactory([llm_result("{not json"), llm_result([CANDIDATE])])
        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session)
            output = runner.run(role="contrarian", prompt="original", schema={})
        self.assertIsInstance(output, list)
        self.assertEqual(factory.calls, 2)
        self.assertIn("Repair the JSON once", factory.prompts[1])
        rows = attempt_rows(session)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["attempt_number"], 1)
        self.assertEqual(rows[0]["status"], "validation_failed")
        self.assertEqual(json.loads(rows[0]["validation_issues"]), ["JSONDecodeError"])
        self.assertEqual(rows[1]["attempt_number"], 2)
        self.assertEqual(rows[1]["status"], "validated")
        self.assertEqual(runner.calls, 2)

    def test_schema_incomplete_candidate_repairs_before_tournament(self):
        refs = [
            "source_claim:claim:0000",
            "source_claim:claim:0001",
            "source_claim:claim:0002",
        ]
        valid = copy.deepcopy(CANDIDATE)
        valid["evidence_refs"] = refs
        valid["citations"] = {field: refs for field in CITATION_FIELDS}
        invalid = copy.deepcopy(valid)
        invalid.pop("sentiment_context")
        session = RecordingSession()
        factory = FakeStageFactory([llm_result([invalid]), llm_result([valid])])

        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session)
            output = runner.run(
                role="fundamental",
                prompt="original",
                schema=role_output_schema(),
            )

        self.assertEqual(output, [valid])
        self.assertEqual(factory.calls, 2)
        self.assertIn("missing required fields: sentiment_context", factory.prompts[1])
        rows = attempt_rows(session)
        self.assertEqual(
            [row["status"] for row in rows], ["validation_failed", "validated"]
        )

    def test_malformed_twice_fails_soft_with_two_distinct_failed_attempts(self):
        session = RecordingSession()
        factory = FakeStageFactory([llm_result("{not json"), llm_result("42")])
        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session)
            with self.assertRaisesRegex(ValueError, "role output failed validation"):
                runner.run(role="editor", prompt="original", schema={})
        self.assertEqual(factory.calls, 2)
        rows = attempt_rows(session)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [row["status"] for row in rows],
            ["validation_failed", "validation_failed"],
        )
        self.assertEqual([row["attempt_number"] for row in rows], [1, 2])
        # Issues are bounded type names, never provider text. The second
        # payload is valid JSON but violates the required array shape.
        self.assertEqual(json.loads(rows[0]["validation_issues"]), ["JSONDecodeError"])
        self.assertEqual(
            json.loads(rows[1]["validation_issues"]),
            ["schema:output must be a JSON array"],
        )

    def test_shape_failure_is_repaired_but_semantic_failures_are_not(self):
        session = RecordingSession()
        factory = FakeStageFactory(
            [llm_result({"not": "an array"}), llm_result([CANDIDATE])]
        )
        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session)
            output = runner.run(role="macro_regime", prompt="p", schema={})
        self.assertIsInstance(output, list)
        rows = attempt_rows(session)
        self.assertEqual(rows[0]["status"], "validation_failed")
        self.assertEqual(
            json.loads(rows[0]["validation_issues"]),
            ["schema:output must be a JSON array"],
        )
        self.assertEqual(rows[1]["status"], "validated")

    def test_challenger_schema_failure_repairs_once(self):
        session = RecordingSession()
        factory = FakeStageFactory(
            [
                llm_result({"kind": "bogus", "statement": "x", "citations": []}),
                llm_result(None),
            ]
        )
        with patch("thesis_autonomy.LLMStage", factory):
            challenger = LLMChallenger(
                {}, correlation_id="corr-1", session=session, budget_cap_usd=1.0
            )
            snapshot = SimpleNamespace(
                thesis_id="t",
                statement="s",
                direction="long",
                as_of=NOW,
                cost=0.0,
                scenarios=(),
                conditions=(),
            )
            self.assertIsNone(challenger.challenge(snapshot, []))
        self.assertEqual(factory.calls, 2)
        rows = attempt_rows(session)
        self.assertEqual(
            [row["status"] for row in rows],
            ["validation_failed", "validated"],
        )
        self.assertEqual(rows[0]["stage"], "challenger")

    def test_challenger_receives_source_titles_and_excerpts(self):
        session = RecordingSession()
        item = evidence_item(1)
        factory = FakeStageFactory([llm_result(None)])
        with patch("thesis_autonomy.LLMStage", factory):
            challenger = LLMChallenger(
                {},
                correlation_id="corr-1",
                session=session,
                evidence_catalog={item.ref: item},
                budget_cap_usd=1.0,
            )
            snapshot = SimpleNamespace(
                thesis_id="t",
                statement="s",
                direction="long",
                as_of=NOW,
                cost=0.0,
                scenarios=(),
                conditions=(),
            )
            self.assertIsNone(challenger.challenge(snapshot, [_signal(item)]))

        self.assertIn(item.title, factory.prompts[0])
        self.assertIn(item.bounded_excerpt, factory.prompts[0])

    def test_auditor_uses_separate_stage_and_records_lineage(self):
        session = RecordingSession()
        decision = [
            {
                "candidate_key": "key-1",
                "verdict": "entailed",
                "cited_refs": ["source_claim:claim:0000"],
                "unsupported_claims": [],
                "rationale": "excerpt supports the claim",
            }
        ]
        factory = FakeStageFactory([llm_result(decision)])
        with patch("thesis_autonomy.LLMStage", factory):
            auditor = LLMSemanticCitationAuditor(
                {}, correlation_id="corr-1", session=session, budget_cap_usd=1.0
            )
            output = auditor.audit(
                candidates=[
                    {
                        "candidate_key": "key-1",
                        "claim": "claim text",
                        "evidence_refs": ["source_claim:claim:0000"],
                    }
                ],
                evidence={},
            )
        self.assertEqual(output, decision)
        self.assertEqual(factory.calls, 1)
        rows = attempt_rows(session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "citation_audit")
        self.assertEqual(rows[0]["status"], "validated")

    def test_auditor_repairs_exactly_once_and_fails_soft_on_double_malformed(self):
        session = RecordingSession()
        decision = [
            {
                "candidate_key": "key-1",
                "verdict": "entailed",
                "cited_refs": [],
                "unsupported_claims": [],
                "rationale": "ok",
            }
        ]
        factory = FakeStageFactory([llm_result("{bad"), llm_result(decision)])
        with patch("thesis_autonomy.LLMStage", factory):
            auditor = LLMSemanticCitationAuditor(
                {}, correlation_id="corr-1", session=session, budget_cap_usd=1.0
            )
            output = auditor.audit(candidates=[], evidence={})
        self.assertEqual(output, decision)
        self.assertEqual(factory.calls, 2)
        rows = attempt_rows(session)
        self.assertEqual([row["attempt_number"] for row in rows], [1, 2])
        self.assertEqual(
            [row["status"] for row in rows], ["validation_failed", "validated"]
        )

        session2 = RecordingSession()
        factory2 = FakeStageFactory([llm_result("{bad"), llm_result("[]x")])
        with patch("thesis_autonomy.LLMStage", factory2):
            auditor2 = LLMSemanticCitationAuditor(
                {}, correlation_id="corr-1", session=session2, budget_cap_usd=1.0
            )
            with self.assertRaisesRegex(ValueError, "citation audit output failed"):
                auditor2.audit(candidates=[], evidence={})
        self.assertEqual(factory2.calls, 2)

    def test_budget_cap_stops_further_calls(self):
        session = RecordingSession()
        factory = FakeStageFactory([llm_result([CANDIDATE], cost_usd=1.5)])
        with patch("thesis_autonomy.LLMStage", factory):
            runner = self._runner(session, budget=1.0)
            with self.assertRaisesRegex(RuntimeError, "budget"):
                runner.run(role="fundamental", prompt="p", schema={})
        self.assertEqual(factory.calls, 1)


class MarketIdentityBackfillTests(unittest.TestCase):
    def test_legacy_null_identity_is_resolved_only_from_linked_evidence(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee growth "
                "through its fee-earning AUM engine"
            ),
        )
        items = [
            evidence_item(
                0,
                entities=[
                    NormalizedEntity.create(
                        "company",
                        "intermediate-capital-group-icg",
                        "Intermediate Capital Group (ICG)",
                    ),
                    NormalizedEntity.create("symbol", "icg-l", "ICG.L"),
                ],
            )
        ]
        session.seed_evidence(EXISTING_ID, evidence_id=items[0].evidence_id)
        catalog = {item.ref: item for item in items}
        changed = _backfill_missing_market_identities(session, catalog)

        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Intermediate Capital Group (ICG)")
        self.assertEqual(thesis["symbol"], "ICG.L")

    def test_unlinked_or_ambiguous_identity_is_never_invented(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim="Acme Corporation should sustain margin expansion",
        )

        changed = _backfill_missing_market_identities(
            session,
            {item.ref: item for item in evidence_items()},
        )

        self.assertEqual(changed, 0)
        self.assertIsNone(thesis["company"])
        self.assertIsNone(thesis["symbol"])

    def test_citation_outside_catalog_recovers_transcript_segment(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-icg:seg1",
        )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_transcripts",
            "institution": "Intermediate Capital Group",
            "document_type": "earnings_call",
            "title": "ICG earnings call",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg-call",
            "content": (
                "Operator: Welcome to the Intermediate Capital Group earnings "
                "call. Management: our fee-earning AUM engine grew this "
                "quarter, and guidance points to continued margin expansion."
            ),
            "metadata": {"ticker": "ICG.L", "available": True},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Intermediate Capital Group")
        self.assertEqual(thesis["symbol"], "ICG.L")

    def test_citation_outside_catalog_recovers_news_document(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Acme Industrial Holdings disclosed a new buyback program "
                "that should support margins"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-acme-news",
        )
        session.recovery_source_documents["doc-acme-news"] = {
            "document_id": "doc-acme-news",
            "source": "issuer_news",
            "institution": "Acme Industrial Holdings",
            "document_type": "issuer_update",
            "title": "Acme announces buyback",
            "published_at": NOW - timedelta(days=500),
            "url": "https://example.test/acme-news",
            "content": "Acme Industrial Holdings announced a buyback program.",
            "metadata": {"ticker": "ACME"},
            "created_at": NOW - timedelta(days=500),
            "updated_at": NOW - timedelta(days=500),
            "acquired_at": NOW - timedelta(days=500),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Acme Industrial Holdings")
        self.assertEqual(thesis["symbol"], "ACME")

    def test_citation_outside_catalog_recovers_filing_delta(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="filing_delta",
            evidence_id="delta-icg-1",
        )
        session.recovery_filing_deltas["delta-icg-1"] = {
            "id": "delta-icg-1",
            "document_id": "doc-icg-10k",
            "previous_document_id": "doc-icg-10k-prev",
            "category": "10-K",
            "change_kind": "modified",
            "section_hash": "h1",
            "previous_section_hash": "h0",
            "excerpt": "Fee-earning AUM grew.",
            "metrics": {},
            "created_at": NOW - timedelta(days=600),
            "company": "Intermediate Capital Group",
            "symbol": "ICG.L",
            "industry": "asset management",
            "region": "GB",
            "report_date": (NOW - timedelta(days=600)).date(),
            "source_url": "https://example.test/icg-10k",
            "filing_source": "sec",
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Intermediate Capital Group")
        self.assertEqual(thesis["symbol"], "ICG.L")

    def test_citation_outside_catalog_recovers_market_bar(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim="ICG.L should re-rate as fee-earning AUM compounds",
        )
        bar_time = NOW - timedelta(days=300)
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="market_confirmation",
            evidence_id=f"ICG.L:1d@{bar_time.isoformat()}",
        )
        session.recovery_market_data[("ICG.L", "1d", bar_time)] = {
            "symbol": "ICG.L",
            "timeframe": "1d",
            "timestamp": bar_time,
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 104.0,
            "volume": 1000.0,
            "source": "test",
            "metadata": {},
            "created_at": bar_time,
            "updated_at": bar_time,
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 1)
        self.assertIsNone(thesis["company"])
        self.assertEqual(thesis["symbol"], "ICG.L")

    def test_catalog_and_recovered_refs_merge_with_catalog_first(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        items = [
            evidence_item(
                0,
                entities=[
                    NormalizedEntity.create(
                        "company",
                        "intermediate-capital-group-icg",
                        "Intermediate Capital Group (ICG)",
                    ),
                    NormalizedEntity.create("symbol", "icg-l", "ICG.L"),
                ],
            )
        ]
        session.seed_evidence(EXISTING_ID, evidence_id=items[0].evidence_id)
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-icg",
        )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        changed = _backfill_missing_market_identities(
            session, {item.ref: item for item in items}, reference=NOW
        )

        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Intermediate Capital Group (ICG)")
        self.assertEqual(thesis["symbol"], "ICG.L")

    def test_recovered_ambiguous_identity_remains_unknown(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Alpha Holdings and Beta Holdings both support margin "
                "expansion this cycle"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-alpha",
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-beta",
        )
        for doc_id, institution, ticker in (
            ("doc-alpha", "Alpha Holdings", "ALPH.L"),
            ("doc-beta", "Beta Holdings", "BET.L"),
        ):
            session.recovery_source_documents[doc_id] = {
                "document_id": doc_id,
                "source": "issuer_news",
                "institution": institution,
                "document_type": "issuer_update",
                "title": f"{institution} update",
                "published_at": NOW - timedelta(days=400),
                "url": f"https://example.test/{doc_id}",
                "content": f"{institution} update",
                "metadata": {"ticker": ticker},
                "created_at": NOW - timedelta(days=400),
                "updated_at": NOW - timedelta(days=400),
                "acquired_at": NOW - timedelta(days=400),
            }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 0)
        self.assertIsNone(thesis["company"])
        self.assertIsNone(thesis["symbol"])

    def test_uncited_persisted_record_never_supplies_identity(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        # The source record exists in persistence, but no citation links it.
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 0)
        self.assertIsNone(thesis["company"])
        self.assertIsNone(thesis["symbol"])

    def test_citation_missing_from_persistence_remains_unknown(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-other",
        )
        # Only an unrelated document is persisted; the cited one is gone.
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 0)
        self.assertIsNone(thesis["company"])
        self.assertIsNone(thesis["symbol"])

    def test_record_persisted_after_reference_is_not_recovered(self):
        session = MemorySession()
        thesis = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-icg",
        )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW + timedelta(days=1),
            "updated_at": NOW + timedelta(days=1),
            "acquired_at": NOW + timedelta(days=1),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 0)
        self.assertIsNone(thesis["company"])
        self.assertIsNone(thesis["symbol"])
        # Without a reference bound the same record is available and
        # resolves (backward-compatible default).
        changed = _backfill_missing_market_identities(session, {})
        self.assertEqual(changed, 1)
        self.assertEqual(thesis["company"], "Intermediate Capital Group")

    def test_same_identity_symbol_canonicalized_different_identity_preserved(self):
        session = MemorySession()
        curated = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            symbol="icg.l",
        )
        different = session.seed_thesis(
            _id("legacy-different-symbol"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            symbol="MSFT",
        )
        for thesis_id in (EXISTING_ID, _id("legacy-different-symbol")):
            session.seed_evidence(
                thesis_id,
                evidence_type="official_document",
                evidence_id="doc-icg",
            )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 2)
        self.assertEqual(curated["company"], "Intermediate Capital Group")
        self.assertEqual(curated["symbol"], "ICG.L")
        self.assertEqual(different["company"], "Intermediate Capital Group")
        self.assertEqual(different["symbol"], "MSFT")

    def test_backfill_with_recovery_is_idempotent(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_type="official_document",
            evidence_id="doc-icg",
        )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        first = _backfill_missing_market_identities(session, {}, reference=NOW)
        second = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

    def _seed_icg_recovery(self, session: MemorySession) -> None:
        """Seed the exact-ID recovery record and its citation."""
        for thesis_id in (
            _id("legacy-post-created"),
            _id("legacy-post-updated"),
            _id("legacy-post-fused"),
            EXISTING_ID,
        ):
            session.seed_evidence(
                thesis_id,
                evidence_type="official_document",
                evidence_id="doc-icg",
            )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }

    def test_post_reference_thesis_never_gets_identity_backfilled(self):
        # An older/stale job must not rewrite identity state on a thesis
        # that did not exist at its reference, was updated after it, or
        # was fused after it; a reference-visible legacy row still
        # backfills exactly once.
        session = MemorySession()
        post_created = session.seed_thesis(
            _id("legacy-post-created"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            created_at=NOW,
            updated_at=NOW,
        )
        post_updated = session.seed_thesis(
            _id("legacy-post-updated"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            created_at=NOW - timedelta(days=2),
            updated_at=NOW,
        )
        post_fused = session.seed_thesis(
            _id("legacy-post-fused"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
            fusion_reference_at=NOW,
        )
        visible = session.seed_thesis(
            EXISTING_ID,
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
        )
        self._seed_icg_recovery(session)

        changed = _backfill_missing_market_identities(
            session, {}, reference=NOW - timedelta(days=1)
        )

        self.assertEqual(changed, 1)
        for thesis in (post_created, post_updated, post_fused):
            self.assertIsNone(thesis["company"])
            self.assertIsNone(thesis["symbol"])
        # The visible legacy row backfills exactly once from the same
        # exact-ID recovery (the excluded rows never mutated it).
        self.assertEqual(visible["company"], "Intermediate Capital Group")
        self.assertEqual(visible["symbol"], "ICG.L")

    def test_missing_thesis_timestamps_fail_closed_for_identity_backfill(self):
        # A thesis without a provable created/updated timestamp is never
        # selected for identity backfill (mirrors the production NULL
        # comparison); the exact cited-evidence point-in-time gate is
        # irrelevant because the thesis row itself is not visible.
        session = MemorySession()
        no_created = session.seed_thesis(
            _id("legacy-no-created"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            created_at=None,
            updated_at=NOW - timedelta(days=2),
        )
        no_updated = session.seed_thesis(
            _id("legacy-no-updated"),
            claim=(
                "Intermediate Capital Group should support management-fee "
                "growth through its fee-earning AUM engine"
            ),
            created_at=NOW - timedelta(days=2),
            updated_at=None,
        )
        for thesis_id in (_id("legacy-no-created"), _id("legacy-no-updated")):
            session.seed_evidence(
                thesis_id,
                evidence_type="official_document",
                evidence_id="doc-icg",
            )
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }

        changed = _backfill_missing_market_identities(session, {}, reference=NOW)

        self.assertEqual(changed, 0)
        self.assertIsNone(no_created["company"])
        self.assertIsNone(no_created["symbol"])
        self.assertIsNone(no_updated["company"])
        self.assertIsNone(no_updated["symbol"])


class ExactRecoverySafetyTests(unittest.TestCase):
    """Exact-ID recovery hardening: PostgreSQL-valid binds, savepoint
    isolation for each physical source query, and the final common
    point-in-time gate that no builder's availability semantics bypass."""

    BAR_AT = datetime(2026, 1, 10, tzinfo=UTC)

    def _seed_all_recovery_sources(self, session: MemorySession) -> list[str]:
        """Seed one valid persisted record per recovery source table and
        return the exact refs that cite them."""
        session.recovery_source_documents["doc-icg"] = {
            "document_id": "doc-icg",
            "source": "issuer_news",
            "institution": "Intermediate Capital Group",
            "document_type": "issuer_update",
            "title": "ICG update",
            "published_at": NOW - timedelta(days=400),
            "url": "https://example.test/icg",
            "content": "ICG update",
            "metadata": {"ticker": "ICG.L"},
            "created_at": NOW - timedelta(days=400),
            "updated_at": NOW - timedelta(days=400),
            "acquired_at": NOW - timedelta(days=400),
        }
        session.recovery_research_source_claims["claim:1"] = {
            "id": "claim:1",
            "evidence_type": "fundamental",
            "evidence_id": None,
            "subject": "Acme Corporation",
            "predicate": "disclosed margin expansion",
            "object_value": None,
            "unit": None,
            "period": None,
            "geography": None,
            "direction": None,
            "claim_kind": "trend",
            "source_span": "Costs are falling",
            "observed_at": NOW - timedelta(days=30),
            "confidence": 0.6,
            "entities": [],
            "model_slug": "test",
            "prompt_version": "v1",
            "input_fingerprint": "fp",
            "provenance": {},
            "created_at": NOW - timedelta(days=30),
        }
        session.recovery_filing_deltas["delta-icg-1"] = {
            "id": "delta-icg-1",
            "document_id": "doc-icg-10k",
            "previous_document_id": "doc-icg-10k-prev",
            "category": "10-K",
            "change_kind": "modified",
            "section_hash": "h1",
            "previous_section_hash": "h0",
            "excerpt": "Fee-earning AUM grew.",
            "metrics": {},
            "created_at": NOW - timedelta(days=600),
            "company": "Intermediate Capital Group",
            "symbol": "ICG.L",
            "industry": "asset management",
            "region": "GB",
            "report_date": (NOW - timedelta(days=600)).date(),
            "source_url": "https://example.test/icg-10k",
            "filing_source": "sec",
        }
        session.recovery_observations["obs-1"] = {
            "observation_id": "obs-1",
            "source_kind": "fundamental",
            "source_id": "src-1",
            "observed_at": NOW - timedelta(days=20),
            "industry": "asset management",
            "company": "Intermediate Capital Group",
            "symbol": "ICG.L",
            "region": "GB",
            "metrics": {},
            "narrative": {"summary": "AUM grew"},
            "themes": [],
            "score": 0.5,
            "state": "current",
            "provenance": {},
            "created_at": NOW - timedelta(days=20),
            "updated_at": NOW - timedelta(days=20),
        }
        session.recovery_analyses["ana-1"] = {
            "analysis_id": "ana-1",
            "document_id": "doc-icg-10k",
            "previous_document_id": "doc-icg-10k-prev",
            "facts": {"metrics": {}, "qualitative": {}},
            "analysis": {"summary": "Margins expand", "state": "complete"},
            "model": "test",
            "created_at": NOW - timedelta(days=15),
            "updated_at": NOW - timedelta(days=15),
            "company": "Intermediate Capital Group",
            "symbol": "ICG.L",
            "industry": "asset management",
            "region": "GB",
            "document_type": "10-K",
            "report_date": (NOW - timedelta(days=600)).date(),
            "source_url": "https://example.test/icg-10k",
            "filing_source": "sec",
        }
        session.recovery_market_data[("ICG.L", "1d", self.BAR_AT)] = {
            "symbol": "ICG.L",
            "timeframe": "1d",
            "timestamp": self.BAR_AT,
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 104.0,
            "volume": 1000.0,
            "source": "test",
            "metadata": {},
            "created_at": self.BAR_AT,
            "updated_at": self.BAR_AT,
        }
        session.recovery_option_snapshots[("cboe", "ICG.L", self.BAR_AT)] = {
            "source": "cboe",
            "symbol": "ICG.L",
            "captured_at": self.BAR_AT,
            "source_timestamp": self.BAR_AT,
            "created_at": self.BAR_AT,
        }
        session.recovery_positioning[
            ("finra", "ICG.L", self.BAR_AT.date(), "short_volume")
        ] = {
            "source": "finra",
            "market_id": "ICG.L",
            "report_date": self.BAR_AT.date(),
            "category": "short_volume",
            "metadata": {"positioning_kind": "short_volume"},
            "created_at": self.BAR_AT,
            "updated_at": self.BAR_AT,
            "acquired_at": self.BAR_AT,
        }
        session.recovery_corporate_actions["ca-1"] = {
            "action_id": "ca-1",
            "symbol": "ICG.L",
            "action_type": "dividend",
            "effective_date": self.BAR_AT.date(),
            "source": "test",
            "source_timestamp": self.BAR_AT,
            "available_at": self.BAR_AT,
            "description": "Q1 dividend",
            "metadata": {},
            "created_at": self.BAR_AT,
        }
        session.recovery_story_clusters["cl-1"] = {
            "id": "cl-1",
            "title": "ICG story",
            "summary": "AUM growth",
            "state": "forming",
            "lane": "single_name",
            "first_seen_at": (self.BAR_AT - timedelta(days=1)).isoformat(),
            "last_seen_at": (self.BAR_AT - timedelta(days=1)).isoformat(),
            "last_material_change_at": (self.BAR_AT - timedelta(days=1)).isoformat(),
            "importance": 0.5,
            "novelty": 0.4,
            "confidence": 0.6,
            "source_count": 3,
            "version": 2,
            "change_summary": None,
            "entities": [],
            "markets": [],
            "clustering_reason": {},
            "updated_at": self.BAR_AT - timedelta(days=1),
        }
        return [
            "official_document:doc-icg",
            "source_claim:claim:1",
            "filing_delta:delta-icg-1",
            "investment_observation:obs-1",
            "investment_analysis:ana-1",
            f"market_confirmation:ICG.L:1d@{self.BAR_AT.isoformat()}",
            f"market_confirmation:cboe:ICG.L@{self.BAR_AT.isoformat()}",
            (
                "market_confirmation:finra:ICG.L:"
                f"{self.BAR_AT.date().isoformat()}:short_volume"
            ),
            "market_confirmation:ca-1",
            "story_cluster:cl-1",
        ]

    def test_recovery_statements_compile_with_postgresql_typed_binds(self):
        session = MemorySession()
        refs = self._seed_all_recovery_sources(session)
        recovered = exact_evidence_lookup(session, refs, available_by=NOW)

        self.assertEqual(set(refs), set(recovered))
        calls = [
            (sql, params)
            for sql, params in session.calls
            if "autonomy_identity_recovery" in sql
        ]
        self.assertGreaterEqual(len(calls), 10)
        self.assertEqual(session.savepoints, len(calls))
        self.assertEqual(session.savepoint_rollbacks, 0)

        array_element_types = {
            "ids": postgresql.TEXT,
            "symbols": postgresql.TEXT,
            "timeframes": postgresql.TEXT,
            "sources": postgresql.TEXT,
            "market_ids": postgresql.TEXT,
            "categories": postgresql.TEXT,
            "report_dates": postgresql.DATE,
            "timestamps": postgresql.TIMESTAMP(timezone=True),
            "captured_ats": postgresql.TIMESTAMP(timezone=True),
        }
        for sql, params in calls:
            # Every supplied parameter must be a recognized text bind; the
            # unsafe ':bind::TYPE[]' spelling silently mis-parses binds, so
            # execution would fail instead of binding the arrays.
            compiled = text(sql).compile(dialect=postgresql.dialect())
            self.assertEqual(set(params), set(compiled.params), sql)
            self.assertNotRegex(sql, r":\w+::(TEXT|TIMESTAMPTZ|DATE)\[")
            if "CAST(:" in sql:
                typed = [
                    bindparam(
                        name,
                        value=(
                            _recovery_literal_value(
                                name, value, array_element_types.get(name)
                            )
                            if name in array_element_types
                            else value
                        ),
                        type_=(
                            postgresql.ARRAY(array_element_types[name])
                            if name in array_element_types
                            else postgresql.TIMESTAMP(timezone=True)
                            if name == "available_by"
                            else None
                        ),
                    )
                    for name, value in params.items()
                ]
                rendered = (
                    text(sql)
                    .bindparams(*typed)
                    .compile(
                        dialect=postgresql.dialect(),
                        compile_kwargs={"literal_binds": True},
                    )
                    .string
                )
                self.assertIn("CAST(ARRAY[", rendered, sql)

    def test_source_query_failure_rolls_back_savepoint_and_keeps_other_sources(self):
        session = MemorySession()
        refs = self._seed_all_recovery_sources(session)
        session.failing_recovery_tables = {"source_documents"}

        recovered = exact_evidence_lookup(session, refs, available_by=NOW)

        self.assertNotIn("official_document:doc-icg", recovered)
        self.assertEqual(set(refs) - {"official_document:doc-icg"}, set(recovered))
        self.assertEqual(session.savepoint_rollbacks, 1)
        # The outer transaction survived: a later lookup on the same
        # session recovers the ref the broken table previously withheld.
        session.failing_recovery_tables = set()
        recovered = exact_evidence_lookup(
            session, ["official_document:doc-icg"], available_by=NOW
        )
        self.assertEqual(set(recovered), {"official_document:doc-icg"})
        self.assertEqual(session.savepoint_rollbacks, 1)

    def test_partial_market_recovery_survives_later_market_table_failure(self):
        session = MemorySession()
        self._seed_all_recovery_sources(session)
        bar_ref = f"market_confirmation:ICG.L:1d@{self.BAR_AT.isoformat()}"
        option_ref = f"market_confirmation:cboe:ICG.L@{self.BAR_AT.isoformat()}"
        corporate_ref = "market_confirmation:ca-1"
        session.failing_recovery_tables = {"option_chain_snapshots"}

        recovered = exact_evidence_lookup(
            session, [bar_ref, option_ref, corporate_ref], available_by=NOW
        )

        self.assertEqual(set(recovered), {bar_ref, corporate_ref})
        self.assertEqual(session.savepoint_rollbacks, 1)

    def test_partial_market_recovery_survives_first_market_table_failure(self):
        session = MemorySession()
        self._seed_all_recovery_sources(session)
        bar_ref = f"market_confirmation:ICG.L:1d@{self.BAR_AT.isoformat()}"
        option_ref = f"market_confirmation:cboe:ICG.L@{self.BAR_AT.isoformat()}"
        corporate_ref = "market_confirmation:ca-1"
        session.failing_recovery_tables = {"market_data"}

        recovered = exact_evidence_lookup(
            session, [bar_ref, option_ref, corporate_ref], available_by=NOW
        )

        self.assertEqual(set(recovered), {option_ref, corporate_ref})
        self.assertEqual(session.savepoint_rollbacks, 1)

    def test_future_source_or_availability_excluded_by_final_cutoff(self):
        session = MemorySession()
        # A claim persisted before the cutoff whose observed timestamp lies
        # after it (the table filter can only bound created_at).
        session.recovery_research_source_claims["claim:future-observed"] = {
            "id": "claim:future-observed",
            "evidence_type": "fundamental",
            "evidence_id": None,
            "subject": "Acme Corporation",
            "predicate": "disclosed margin expansion",
            "object_value": None,
            "unit": None,
            "period": None,
            "geography": None,
            "direction": None,
            "claim_kind": "trend",
            "source_span": "Costs are falling",
            "observed_at": NOW + timedelta(days=1),
            "confidence": 0.6,
            "entities": [],
            "model_slug": "test",
            "prompt_version": "v1",
            "input_fingerprint": "fp",
            "provenance": {},
            "created_at": NOW - timedelta(days=30),
        }
        # A story version persisted before the cutoff whose snapshot
        # last_seen_at lies after it.
        session.recovery_story_clusters["cl-future"] = {
            "id": "cl-future",
            "title": "Future story",
            "summary": "Later sighting",
            "state": "forming",
            "lane": "single_name",
            "first_seen_at": (NOW - timedelta(days=2)).isoformat(),
            "last_seen_at": (NOW + timedelta(days=1)).isoformat(),
            "last_material_change_at": (NOW - timedelta(days=2)).isoformat(),
            "importance": 0.5,
            "novelty": 0.4,
            "confidence": 0.6,
            "source_count": 3,
            "version": 2,
            "change_summary": None,
            "entities": [],
            "markets": [],
            "clustering_reason": {},
            "updated_at": NOW - timedelta(days=2),
        }
        # A corporate action whose source time predates the cutoff but whose
        # availability time does not.
        session.recovery_corporate_actions["ca-future"] = {
            "action_id": "ca-future",
            "symbol": "ICG.L",
            "action_type": "dividend",
            "effective_date": (NOW - timedelta(days=2)).date(),
            "source": "test",
            "source_timestamp": NOW - timedelta(days=2),
            "available_at": NOW + timedelta(days=1),
            "description": "Q1 dividend",
            "metadata": {},
            "created_at": NOW - timedelta(days=2),
        }
        refs = [
            "source_claim:claim:future-observed",
            "story_cluster:cl-future",
            "market_confirmation:ca-future",
        ]

        bounded = exact_evidence_lookup(session, refs, available_by=NOW)
        self.assertEqual(bounded, {})
        self.assertEqual(session.savepoint_rollbacks, 0)
        # Without a cutoff the same records satisfy the contract
        # (backward-compatible default), proving the final gate alone
        # excludes them while bounded.
        unbounded = exact_evidence_lookup(session, refs)
        self.assertEqual(set(unbounded), set(refs))


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


class CandidateCatalystIdentityTests(unittest.TestCase):
    """Guarded exact-identity persistence for generated catalysts."""

    def test_guard_takes_namespaced_identity_lock_before_the_insert(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        changed = _ensure_candidate_catalyst(
            session, EXISTING_ID, "  A   quarterly disclosure  "
        )
        self.assertTrue(changed)
        # The exact identity lock is namespaced apart from the fusion
        # canonical-key lock and precedes the insert.
        self.assertEqual(
            session.catalyst_lock_keys,
            [f"catalyst_identity:{EXISTING_ID}:A quarterly disclosure"],
        )
        # Global lock order: the fusion canonical-key lock (merge's exact
        # lock) comes first, then the catalyst identity lock, then the
        # guarded insert — a catalyst lock is never retained across a
        # later fusion acquisition.
        fusion_index = next(
            index
            for index, (sql, params) in enumerate(session.calls)
            if "pg_advisory_xact_lock" in sql
            and "catalyst_identity_lock" not in sql
            and params.get("key") == f"key:{EXISTING_ID}"
        )
        lock_index = next(
            index
            for index, (sql, _params) in enumerate(session.calls)
            if "catalyst_identity_lock" in sql
        )
        insert_index = next(
            index
            for index, (sql, _params) in enumerate(session.calls)
            if sql.startswith("INSERT INTO investment_catalysts")
        )
        self.assertLess(fusion_index, lock_index)
        self.assertLess(lock_index, insert_index)
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["state"], "pending")

    def test_legacy_thesis_without_canonical_key_skips_the_fusion_lock(self):
        # A legacy thesis with no canonical identity cannot be merged by a
        # concurrent cycle, so the guarded helper takes only the catalyst
        # identity lock: no fusion lock to deadlock against.
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, canonical_key=None)
        self.assertTrue(
            _ensure_candidate_catalyst(session, EXISTING_ID, "Legacy catalyst")
        )
        self.assertEqual(
            session.catalyst_lock_keys,
            [f"catalyst_identity:{EXISTING_ID}:Legacy catalyst"],
        )
        self.assertFalse(
            any(
                "pg_advisory_xact_lock" in sql and "catalyst_identity_lock" not in sql
                for sql, _params in session.calls
            )
        )
        self.assertEqual(len(session.catalysts), 1)

    def test_rerun_takes_the_lock_again_and_reports_a_truthful_noop(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        description = "Quarterly disclosure confirms the operating change"
        self.assertTrue(_ensure_candidate_catalyst(session, EXISTING_ID, description))
        self.assertFalse(_ensure_candidate_catalyst(session, EXISTING_ID, description))
        # No-op reruns still serialize on the same identity lock, so a
        # concurrent rerun cannot slip past a fresh insert.
        self.assertEqual(
            session.catalyst_lock_keys,
            [
                f"catalyst_identity:{EXISTING_ID}:{description}",
                f"catalyst_identity:{EXISTING_ID}:{description}",
            ],
        )
        self.assertEqual(len(session.catalysts), 1)

    def test_distinct_descriptions_stay_separate_rows(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        self.assertTrue(
            _ensure_candidate_catalyst(session, EXISTING_ID, "First catalyst")
        )
        self.assertTrue(
            _ensure_candidate_catalyst(session, EXISTING_ID, "Second catalyst")
        )
        # Different identities lock different keys and persist separately.
        self.assertEqual(
            session.catalyst_lock_keys,
            [
                f"catalyst_identity:{EXISTING_ID}:First catalyst",
                f"catalyst_identity:{EXISTING_ID}:Second catalyst",
            ],
        )
        self.assertEqual(len(session.catalysts), 2)

    def test_blank_description_is_rejected_before_taking_the_lock(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)
        for blank in (None, "", "   "):
            self.assertFalse(_ensure_candidate_catalyst(session, EXISTING_ID, blank))
        self.assertEqual(session.catalyst_lock_keys, [])
        self.assertEqual(len(session.catalysts), 0)


class GeneratedCatalystBackfillTests(unittest.TestCase):
    def test_reference_visible_legacy_catalyst_is_materialized_once(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            catalyst_summary="Quarterly disclosure confirms the operating change",
        )

        first = _backfill_generated_catalysts(session, NOW)
        second = _backfill_generated_catalysts(session, NOW)

        # (thesis_id, normalized summary) pairs feed the current-cycle
        # explicit scoring inputs for the backfilled catalysts.
        self.assertEqual(
            first,
            (
                (
                    EXISTING_ID,
                    "Quarterly disclosure confirms the operating change",
                ),
            ),
        )
        self.assertEqual(second, ())
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["state"], "pending")

    def test_post_reference_created_thesis_is_never_backfilled(self):
        # A replay at R must not materialize a catalyst for a thesis that
        # did not exist at R; a reference-visible thesis still backfills.
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            catalyst_summary="Quarterly disclosure confirms the operating change",
            created_at=NOW,
            updated_at=NOW,
        )
        visible_id = _id("legacy-visible-catalyst")
        session.seed_thesis(
            visible_id,
            catalyst_summary="Visible legacy summary",
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
        )

        result = _backfill_generated_catalysts(session, NOW - timedelta(days=1))

        self.assertEqual(result, ((visible_id, "Visible legacy summary"),))
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["thesis_id"], visible_id)

    def test_post_reference_updated_thesis_is_never_backfilled(self):
        # An older job must not materialize catalyst state on a thesis a
        # newer cycle updated after the reference (existence alone is not
        # enough: the current row must be provably visible at R).
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            catalyst_summary="Quarterly disclosure confirms the operating change",
            created_at=NOW - timedelta(days=2),
            updated_at=NOW,
        )
        visible_id = _id("legacy-visible-catalyst")
        session.seed_thesis(
            visible_id,
            catalyst_summary="Visible legacy summary",
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
        )

        result = _backfill_generated_catalysts(session, NOW - timedelta(days=1))

        self.assertEqual(result, ((visible_id, "Visible legacy summary"),))
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["thesis_id"], visible_id)

    def test_future_fusion_reference_thesis_is_never_backfilled(self):
        # A thesis claimed by a newer accepted reference is never
        # backfilled by an older replay, whatever its lifecycle timestamps.
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            catalyst_summary="Quarterly disclosure confirms the operating change",
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
            fusion_reference_at=NOW,
        )
        visible_id = _id("legacy-visible-catalyst")
        session.seed_thesis(
            visible_id,
            catalyst_summary="Visible legacy summary",
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
        )

        result = _backfill_generated_catalysts(session, NOW - timedelta(days=1))

        self.assertEqual(result, ((visible_id, "Visible legacy summary"),))
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["thesis_id"], visible_id)

    def test_missing_timestamps_fail_closed_for_catalyst_backfill(self):
        # A thesis without a provable created/updated timestamp is never
        # selected for backfill (mirrors the production NULL comparison).
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            catalyst_summary="Quarterly disclosure confirms the operating change",
            created_at=None,
            updated_at=NOW - timedelta(days=2),
        )
        session.seed_thesis(
            _id("legacy-no-updated"),
            catalyst_summary="Another legacy summary",
            created_at=NOW - timedelta(days=2),
            updated_at=None,
        )
        session.seed_thesis(
            _id("legacy-no-created"),
            catalyst_summary="Yet another legacy summary",
            created_at=None,
            updated_at=None,
        )

        result = _backfill_generated_catalysts(session, NOW)

        self.assertEqual(result, ())
        self.assertEqual(session.catalysts, [])


class AutonomousCycleTests(unittest.TestCase):
    def test_credentialless_cycle_persists_ranks_scores_and_falsifies(self):
        session = MemorySession()
        runner = ScriptedRunner(CANDIDATE)
        challenger = ScriptedChallenger()
        auditor = ScriptedAuditor()
        result = run_cycle(
            session, runner=runner, challenger=challenger, auditor=auditor
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["evidence_collected"], 6)
        self.assertEqual(result["raw_candidate_count"], 8)
        self.assertEqual(result["promoted_count"], 1)
        self.assertEqual(result["thesis_count"], 1)
        self.assertEqual(result["scenario_upserts"], 3)
        self.assertEqual(result["risk_upserts"], 1)
        self.assertEqual(result["catalyst_upserts"], 1)
        self.assertEqual(result["group_count"], 1)
        self.assertEqual(result["falsification_runs"], 1)
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(result["second_pass_candidates"], 0)
        self.assertEqual(result["role_failures"], 0)
        self.assertEqual(result["challenger_failures"], 0)
        self.assertEqual(result["semantic_audit_rejections"], 0)
        self.assertEqual(result["cost_usd"], 0.08)
        self.assertEqual(result["theme_id"], THEME_ID)
        self.assertTrue(result["theme_created"])
        self.assertEqual(result["cycle_key"], "20260815T093000.000000")

        self.assertEqual(len(session.theses), 1)
        thesis = next(iter(session.theses.values()))
        self.assertEqual(thesis["status"], "candidate")
        self.assertEqual(thesis["company"], "acme-corporation")
        self.assertEqual(thesis["symbol"], "ACME")
        active_scenarios = [
            row for row in session.scenarios if row["superseded_at"] is None
        ]
        self.assertEqual(
            {row["name"] for row in active_scenarios}, {"bull", "base", "bear"}
        )
        base = next(row for row in active_scenarios if row["name"] == "base")
        self.assertTrue(base["is_base_case"])
        self.assertEqual(
            {row["expected_return"] for row in active_scenarios}, {0.1, 0.0, -0.2}
        )
        self.assertEqual(
            {row["name"]: row["description"] for row in active_scenarios},
            {
                "bull": CANDIDATE["scenarios"]["bull"]["description"],
                "base": CANDIDATE["scenarios"]["base"]["description"],
                "bear": CANDIDATE["scenarios"]["bear"]["description"],
            },
        )
        # Each supplied invalidator materializes as one structured risk row
        # with conservative deterministic kind/severity — never invented.
        self.assertEqual(len(session.risks), 1)
        risk = session.risks[0]
        self.assertEqual(risk["thesis_id"], thesis["id"])
        self.assertEqual(risk["description"], CANDIDATE["invalidators"][0])
        self.assertEqual(risk["kind"], "counter_thesis")
        self.assertEqual(risk["severity"], "moderate")
        self.assertEqual(len(session.groups), 1)
        self.assertEqual(len(session.members), 1)
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["state"], "pending")
        self.assertEqual(len(session.evidence), 3)
        self.assertTrue(
            all(row["relationship"] == "supports" for row in session.evidence)
        )
        self.assertTrue(all(row["excerpt"] for row in session.evidence))
        self.assertTrue(
            all(
                row["quality_score"] is not None
                and row["entailment_score"] is not None
                and row["freshness_score"] is not None
                and row["effective_weight"] is not None
                for row in session.evidence
            )
        )
        self.assertTrue(all(row["entailment_score"] == 1.0 for row in session.evidence))
        self.assertEqual(len(session.snapshots), 1)
        self.assertEqual(len(session.falsification_runs), 1)
        run = next(iter(session.falsification_runs.values()))
        self.assertEqual(run["status"], "not_falsified")
        self.assertEqual(result["opportunity_snapshots"], 1)
        self.assertEqual(runner.calls, 8)
        self.assertEqual(auditor.calls, 1)

    def test_nullable_scenario_probabilities_persist_as_unknown(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["scenarios"]["base"]["probability"] = None
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(candidate))
        self.assertEqual(result["status"], "completed")
        active_scenarios = [
            row for row in session.scenarios if row["superseded_at"] is None
        ]
        base = next(row for row in active_scenarios if row["name"] == "base")
        self.assertIsNone(base["probability"])
        run = next(iter(session.falsification_runs.values()))
        # Missing probability threatens (never invented): inconclusive.
        self.assertEqual(run["status"], "inconclusive")

    def test_rerun_with_identical_inputs_creates_no_duplicate_identities(self):
        session = MemorySession()
        runner = ScriptedRunner(CANDIDATE)
        first = run_cycle(session, runner=runner, challenger=ScriptedChallenger())
        second = run_cycle(session, runner=runner, challenger=ScriptedChallenger())

        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        self.assertEqual(second["thesis_count"], 1)
        self.assertEqual(second["scenario_upserts"], 0)
        self.assertEqual(second["risk_upserts"], 0)
        self.assertEqual(second["falsification_runs"], 0)
        self.assertEqual(second["playbook_upserts"], 0)
        self.assertEqual(second["catalyst_upserts"], 0)
        self.assertFalse(second["theme_created"])

        self.assertEqual(len(session.theses), 1)
        self.assertEqual(len(session.evidence), 3)
        self.assertEqual(
            len([row for row in session.scenarios if row["superseded_at"] is None]),
            3,
        )
        # Reruns never duplicate structured risks: the single invalidator
        # stays one row with the same identity.
        self.assertEqual(len(session.risks), 1)
        self.assertEqual(len(session.snapshots), 1)
        self.assertEqual(len(session.falsification_runs), 1)
        self.assertEqual(len(session.groups), 1)
        self.assertEqual(len(session.members), 1)
        self.assertEqual(len(session.playbooks), 1)
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(first["cycle_key"], second["cycle_key"])

    def test_current_cycle_scores_its_own_persisted_artifacts_at_cutoff(self):
        # The cycle reference precedes the run's own writes: scenarios and
        # the catalyst are persisted DURING the run, so their rows postdate
        # the cutoff and would be excluded by the replay-safe persisted
        # queries.  They enter scoring as explicit current-cycle inputs
        # (derived from cutoff-bounded evidence), so the fresh candidate
        # still scores its own legs, catalyst, and opportunity snapshot.
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result = run_cycle(
            session,
            as_of=NOW - timedelta(seconds=1),
            runner=ScriptedRunner(CANDIDATE),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["opportunity_snapshots"], 1)
        thesis = next(iter(session.theses.values()))
        # 0.3*0.1 + 0.5*0.0 + 0.2*(-0.2) - 0.0 cost from the candidate legs;
        # a DB-only reconstruction at the cutoff would find no scenarios
        # (rows were created after the reference) and value the thesis at 0.
        self.assertAlmostEqual(thesis["expected_value"], -0.01)
        self.assertAlmostEqual(thesis["expected_shortfall"], 0.04)
        # One pending catalyst without a date contributes the neutral 0.5.
        self.assertEqual(thesis["catalyst_score"], 0.5)
        self.assertGreater(thesis["evidence_strength"], 0.0)
        self.assertGreater(thesis["opportunity_score"], 0.0)

    def test_stale_replay_evaluation_does_not_regress_current_scores(self):
        # A replay cycle whose reference predates a newer evaluation of the
        # same thesis computes its results but must not overwrite the newer
        # current ranking columns or last_evaluated_at.  The legacy catalyst
        # backfill evaluation is the cycle path that touches an
        # already-evaluated thesis (its catalyst row postdates the
        # reference, so it scores via the explicit current-cycle input).
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            origin="fusion",
            status="active",
            catalyst_summary="Quarterly disclosure confirms the operating change",
            opportunity_score=0.9,
            last_evaluated_at=NOW,
        )
        result = run_cycle(
            session,
            as_of=NOW - timedelta(days=1),
            runner=ScriptedRunner(CANDIDATE),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["legacy_catalyst_backfills"], 1)
        thesis = session.theses[EXISTING_ID]
        # The newer evaluation stays the current ranking state; the stale
        # replay result (computed and returned) is never applied.
        self.assertEqual(thesis["last_evaluated_at"], NOW)
        self.assertEqual(thesis["opportunity_score"], 0.9)

    def test_replay_never_backfills_or_scores_post_reference_catalyst(self):
        # A replay at R performs no maintenance write and supplies no
        # explicit score input for a thesis whose existence/current/fusion
        # state is not provable at R: no catalyst row is materialized for
        # it and it is never re-evaluated from the older cycle (its newer
        # ranking state stays untouched).
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            origin="fusion",
            status="active",
            catalyst_summary="Quarterly disclosure confirms the operating change",
            created_at=NOW,
            updated_at=NOW,
            fusion_reference_at=NOW,
            last_evaluated_at=NOW,
            opportunity_score=0.9,
        )
        result = run_cycle(
            session,
            as_of=NOW - timedelta(days=1),
            runner=ScriptedRunner(CANDIDATE),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["legacy_catalyst_backfills"], 0)
        thesis = session.theses[EXISTING_ID]
        # No score backdating: the thesis was never evaluated by the replay.
        self.assertEqual(thesis["last_evaluated_at"], NOW)
        self.assertEqual(thesis["opportunity_score"], 0.9)
        self.assertFalse(
            any(row["thesis_id"] == EXISTING_ID for row in session.catalysts)
        )

    def test_role_failure_is_isolated_and_other_roles_still_promote(self):
        session = MemorySession()
        runner = ScriptedRunner(CANDIDATE, fail_roles={"contrarian"})
        result = run_cycle(session, runner=runner)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["role_failures"], 1)
        self.assertEqual(result["promoted_count"], 1)
        self.assertEqual(len(session.theses), 1)

    def test_all_roles_failing_still_returns_bounded_result(self):
        class FailingRunner:
            cost_usd = 0.0
            calls = 0

            def run(self, *, role, prompt, schema):
                raise RuntimeError("simulated outage")

        session = MemorySession()
        result = run_cycle(session, runner=FailingRunner())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["role_failures"], 8)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["thesis_count"], 0)
        self.assertEqual(result["error_count"], 0)

    def test_challenger_failure_fails_closed_before_promotion(self):
        session = MemorySession()
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=ScriptedChallenger(fail=True),
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["challenger_failures"], 1)
        self.assertEqual(result["falsification_runs"], 0)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(session.falsification_runs, {})

    def test_source_quality_gate_rejects_single_family_support(self):
        same_family = tuple(
            evidence_item(
                index,
                source_name="One Wire",
                provenance={
                    "adapter": "source_claims",
                    "source_family": "one-wire",
                },
            )
            for index in range(6)
        )
        with patch.object(
            EvidenceRegistry,
            "collect",
            return_value=EvidenceCollection(items=same_family, failures={}),
        ):
            result = run_autonomous_thesis_cycle(
                MemorySession(),
                cycle_config(
                    minimum_supporting_source_families=2,
                    require_cited_excerpts=True,
                ),
                as_of=NOW,
                runner=ScriptedRunner(CANDIDATE),
                challenger=ScriptedChallenger(),
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["source_gate_rejections"], 1)

    def test_opposition_gate_rejects_one_sided_competition(self):
        with patch.object(
            EvidenceRegistry,
            "collect",
            return_value=EvidenceCollection(items=tuple(evidence_items()), failures={}),
        ):
            result = run_autonomous_thesis_cycle(
                MemorySession(),
                cycle_config(require_opposing_variants=True),
                as_of=NOW,
                runner=ScriptedRunner(CANDIDATE),
                challenger=ScriptedChallenger(),
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["opposition_gate_rejections"], 1)

    def test_rejected_opponent_cannot_enable_surviving_direction(self):
        class RejectShortChallenger(ScriptedChallenger):
            def challenge(self, snapshot, evidence):
                self.calls += 1
                if snapshot.direction == "short":
                    raise RuntimeError("short challenger unavailable")
                return None

        session = MemorySession()
        challenger = RejectShortChallenger()
        with patch.object(
            EvidenceRegistry,
            "collect",
            return_value=EvidenceCollection(items=tuple(evidence_items()), failures={}),
        ):
            result = run_autonomous_thesis_cycle(
                session,
                cycle_config(require_opposing_variants=True),
                as_of=NOW,
                runner=OpposingRunner(CANDIDATE),
                challenger=challenger,
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["challenger_failures"], 1)
        self.assertEqual(result["opposition_gate_rejections"], 1)
        self.assertEqual(result["challenge_attempts"], 2)
        self.assertEqual(session.theses, {})

    def test_opposing_variants_share_one_canonical_competition_group(self):
        session = MemorySession()
        with (
            patch.object(
                EvidenceRegistry,
                "collect",
                return_value=EvidenceCollection(
                    items=tuple(evidence_items()), failures={}
                ),
            ),
            patch(
                "thesis_autonomy._existing_fusion_mechanism",
                side_effect=lambda *args, **kwargs: kwargs["fallback"],
            ),
        ):
            result = run_autonomous_thesis_cycle(
                session,
                cycle_config(
                    minimum_supporting_source_families=2,
                    require_cited_excerpts=True,
                    require_opposing_variants=True,
                ),
                as_of=NOW,
                runner=OpposingRunner(CANDIDATE),
                challenger=ScriptedChallenger(),
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["promoted_count"], 2)
        self.assertEqual(result["group_count"], 1)
        self.assertEqual(
            {row["direction"] for row in session.theses.values()}, {"long", "short"}
        )

    def test_auditor_gates_promotion_and_failures_reject_the_batch(self):
        session = MemorySession()
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            auditor=ScriptedAuditor(verdict="unsupported"),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["semantic_audit_rejections"], 1)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["thesis_count"], 0)

        session2 = MemorySession()
        result2 = run_cycle(
            session2,
            runner=ScriptedRunner(CANDIDATE),
            auditor=ScriptedAuditor(fail=True),
        )
        self.assertEqual(result2["status"], "completed")
        self.assertEqual(result2["promoted_count"], 0)
        self.assertEqual(result2["error_count"], 0)

    def test_challenger_citations_attach_as_contradicts_and_score_recomputes(self):
        session = MemorySession()
        proposal = {
            "kind": "counter_evidence",
            "statement": "The cited evidence supports the opposite view",
            "citations": ["source_claim:claim:0005"],
        }
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=ScriptedChallenger(proposal=proposal),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["contradictions_attached"], 1)
        self.assertEqual(result["challenger_failures"], 0)
        contradicts = [
            row for row in session.evidence if row["relationship"] == "contradicts"
        ]
        self.assertEqual(len(contradicts), 1)
        self.assertEqual(contradicts[0]["evidence_id"], "claim:0005")
        run = next(iter(session.falsification_runs.values()))
        self.assertEqual(run["status"], "inconclusive")

    def test_breached_existing_thesis_pauses_and_never_closes(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            status="active",
            opportunity_score=0.9,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:invalidate",
            relationship="invalidation",
            evidence_fingerprint="d" * 64,
        )
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")
        run = session.falsification_runs[
            (EXISTING_ID, "autonomy:20260815T093000.000000")
        ]
        self.assertEqual(run["status"], "falsified")
        # The promoted thesis stays candidate: intact theses never auto-promote.
        promoted = [row for row in session.theses.values() if row["id"] != EXISTING_ID]
        self.assertEqual(promoted[0]["status"], "candidate")

    def test_second_pass_skips_theses_already_challenged_this_cycle(self):
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        # The only candidate thesis is the promoted one; it is excluded from
        # the second pass, so nothing is challenged twice in one run.
        self.assertEqual(result["second_pass_candidates"], 0)
        self.assertEqual(result["second_pass_challenged"], 0)
        self.assertEqual(len(session.falsification_runs), 1)

    def test_second_pass_recompute_keeps_backfilled_catalyst_at_cutoff(self):
        # A legacy thesis whose catalyst is backfilled this cycle is scored
        # with the catalyst as an explicit current-cycle input; when its
        # contradiction recompute runs in the second pass, the same explicit
        # input must survive the replay cutoff (the catalyst row postdates
        # the reference) instead of being dropped and erasing the score.
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            origin="fusion",
            status="active",
            opportunity_score=0.9,
            catalyst_summary="Quarterly disclosure confirms the operating change",
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:seed",
            evidence_fingerprint="c" * 64,
        )
        proposal = {
            "kind": "counter_evidence",
            "statement": "The cited evidence supports the opposite view",
            "citations": ["source_claim:claim:0000"],
        }
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=ScriptedChallenger(proposal=proposal),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["legacy_catalyst_backfills"], 1)
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        # The promoted thesis already cites claim:0000 as support, so the
        # contradiction is deduplicated there and only the second-pass
        # thesis gains a contradicts row (which triggers its recompute).
        self.assertEqual(result["contradictions_attached"], 1)
        self.assertEqual(
            [
                row["thesis_id"]
                for row in session.evidence
                if row["relationship"] == "contradicts"
            ],
            [EXISTING_ID],
        )
        # The recompute kept the backfilled catalyst: pending scores 0.5.
        self.assertEqual(session.theses[EXISTING_ID]["catalyst_score"], 0.5)


class CandidateRiskPersistenceTests(unittest.TestCase):
    """Promoted candidates persist described scenarios and structured risks."""

    def test_blank_scenario_description_rejects_promotion(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["scenarios"]["bull"]["description"] = "   "
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(candidate))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        # The blank description rejects the candidate before promotion: no
        # investable row, no scenarios, no risks.
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["thesis_count"], 0)
        self.assertEqual(session.theses, {})
        self.assertEqual(session.scenarios, [])
        self.assertEqual(session.risks, [])

    def test_empty_invalidators_reject_promotion(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["invalidators"] = []
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(candidate))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        # No supplied invalidator means no structured risk could exist, so
        # promotion is rejected rather than creating an investable row.
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["thesis_count"], 0)
        self.assertEqual(session.theses, {})
        self.assertEqual(session.risks, [])

    def test_risks_are_idempotent_and_never_invented(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)

        class Candidate:
            def __init__(self, invalidators):
                self.invalidators = tuple(invalidators)

        first = _persist_candidate_risks(
            session,
            EXISTING_ID,
            Candidate(["cost trend reverses", "guidance withdrawn"]),
        )
        second = _persist_candidate_risks(
            session,
            EXISTING_ID,
            Candidate(["cost trend reverses", "guidance withdrawn"]),
        )
        # One row per supplied invalidator, exactly; reruns add nothing.
        self.assertEqual(first, 2)
        self.assertEqual(second, 0)
        self.assertEqual(len(session.risks), 2)
        self.assertEqual(
            {row["description"] for row in session.risks},
            {"cost trend reverses", "guidance withdrawn"},
        )
        # Conservative deterministic kind/severity, never inferred.
        self.assertTrue(all(row["kind"] == "counter_thesis" for row in session.risks))
        self.assertTrue(all(row["severity"] == "moderate" for row in session.risks))
        # Every insert serialized on the exact-identity advisory lock.
        self.assertEqual(len(session.risk_lock_keys), 4)
        self.assertTrue(
            all(
                key.startswith(f"risk_identity:{EXISTING_ID}:")
                for key in session.risk_lock_keys
            )
        )


class CycleIdentityTests(unittest.TestCase):
    """The cycle identity is the full accepted reference (seconds plus
    fractional microseconds, UTC): distinct accepted references never
    collide on snapshots, falsification runs, or challenge claims, while an
    exact-reference rerun coalesces on the same keys."""

    def test_cycle_key_renders_fixed_full_precision_utc(self):
        # Seconds and fractional microseconds are part of the identity, so
        # 09:30:10, 09:30:50, and 09:30:10.000001 never share a key.
        self.assertEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, tzinfo=UTC)),
            "20260815T093010.000000",
        )
        self.assertEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 50, tzinfo=UTC)),
            "20260815T093050.000000",
        )
        self.assertEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, 1, tzinfo=UTC)),
            "20260815T093010.000001",
        )
        self.assertEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, 123456, tzinfo=UTC)),
            "20260815T093010.123456",
        )
        # Aware non-UTC references normalize to UTC first.
        self.assertEqual(
            _cycle_key(datetime.fromisoformat("2026-08-15T11:30:10+02:00")),
            "20260815T093010.000000",
        )
        # Naive references are treated as UTC, exactly like _as_utc.
        self.assertEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10)),
            "20260815T093010.000000",
        )
        # Deterministic, fixed width, and collision-free across seconds and
        # fractional microseconds.
        key = _cycle_key(datetime(2026, 8, 15, 9, 30, 10, 123456, tzinfo=UTC))
        self.assertEqual(len(key), 22)
        self.assertEqual(
            key,
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, 123456, tzinfo=UTC)),
        )
        self.assertNotEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, tzinfo=UTC)),
            _cycle_key(datetime(2026, 8, 15, 9, 30, 50, tzinfo=UTC)),
        )
        self.assertNotEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, tzinfo=UTC)),
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, 1, tzinfo=UTC)),
        )

    def test_seconds_apart_references_never_share_audit_identities(self):
        # 09:30:00 and 09:30:40 are distinct accepted references: the
        # second cycle must append its own immutable snapshot, falsification
        # run, and challenge claim instead of colliding with (or
        # overwriting) the first cycle's rows.
        session = MemorySession()
        challenger = RecordingChallenger()
        first = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=challenger,
        )
        second = run_cycle(
            session,
            as_of=NOW + timedelta(seconds=40),
            runner=ScriptedRunner(CANDIDATE),
            challenger=challenger,
        )

        self.assertEqual(first["cycle_key"], "20260815T093000.000000")
        self.assertEqual(second["cycle_key"], "20260815T093040.000000")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        (thesis_id,) = session.theses.keys()
        # Both accepted references left their own immutable audit rows.
        self.assertEqual(
            session.snapshots,
            {
                (thesis_id, "autonomy:20260815T093000.000000"),
                (thesis_id, "autonomy:20260815T093040.000000"),
            },
        )
        self.assertEqual(
            set(session.falsification_runs),
            {
                (thesis_id, "autonomy:20260815T093000.000000"),
                (thesis_id, "autonomy:20260815T093040.000000"),
            },
        )
        self.assertEqual(len(session.falsification_runs), 2)
        # Challenge claim identities embed the full accepted reference too.
        self.assertEqual(len(challenger.captured), 2)
        claims = [snapshot.claims[0].claim_id for snapshot, _ in challenger.captured]
        candidate_id = claims[0].split(":autonomy:", 1)[0]
        self.assertEqual(
            set(claims),
            {
                f"{candidate_id}:autonomy:20260815T093000.000000",
                f"{candidate_id}:autonomy:20260815T093040.000000",
            },
        )

    def test_fractional_microsecond_references_stay_distinct(self):
        # 09:30:10.000000 vs 09:30:10.000001: even a one-microsecond
        # difference must never collide or overwrite by timing.
        session = MemorySession()
        first = run_cycle(
            session,
            as_of=NOW + timedelta(seconds=10),
            runner=ScriptedRunner(CANDIDATE),
        )
        second = run_cycle(
            session,
            as_of=NOW + timedelta(seconds=10, microseconds=1),
            runner=ScriptedRunner(CANDIDATE),
        )

        self.assertEqual(first["cycle_key"], "20260815T093010.000000")
        self.assertEqual(second["cycle_key"], "20260815T093010.000001")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        (thesis_id,) = session.theses.keys()
        self.assertEqual(
            session.snapshots,
            {
                (thesis_id, "autonomy:20260815T093010.000000"),
                (thesis_id, "autonomy:20260815T093010.000001"),
            },
        )
        self.assertEqual(
            set(session.falsification_runs),
            {
                (thesis_id, "autonomy:20260815T093010.000000"),
                (thesis_id, "autonomy:20260815T093010.000001"),
            },
        )

    def test_exact_reference_rerun_coalesces_on_the_same_audit_keys(self):
        # An exact rerun at one accepted reference reproduces the identical
        # cycle identity and idempotently reuses the snapshot and
        # falsification rows instead of appending duplicates.
        session = MemorySession()
        first = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        second = run_cycle(session, runner=ScriptedRunner(CANDIDATE))

        self.assertEqual(first["cycle_key"], "20260815T093000.000000")
        self.assertEqual(first["cycle_key"], second["cycle_key"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        self.assertEqual(second["falsification_runs"], 0)
        (thesis_id,) = session.theses.keys()
        self.assertEqual(
            session.snapshots,
            {(thesis_id, "autonomy:20260815T093000.000000")},
        )
        self.assertEqual(
            set(session.falsification_runs),
            {(thesis_id, "autonomy:20260815T093000.000000")},
        )


class FusionReferenceRaceTests(unittest.TestCase):
    """Accepted reference, not completion order, decides current state."""

    OLDER = NOW - timedelta(days=1)

    def _older_candidate(self) -> dict:
        candidate = copy.deepcopy(CANDIDATE)
        candidate["claim"] = "An older cycle's stale claim for the subject"
        candidate["variant_perception"] = "Superseded variant perception"
        # Every leg differs from the current candidate (including base,
        # whose 0.5/0.0 would otherwise be an intentional no-op), so the
        # newer cycle's upserts supersede ALL older versions; probabilities
        # stay valid and sum to one across bull/base/bear.
        candidate["scenarios"] = {
            "bull": {
                "probability": 0.2,
                "expected_return": 0.05,
                "description": "older bull path",
            },
            "base": {
                "probability": 0.6,
                "expected_return": 0.02,
                "description": "older base path",
            },
            "bear": {
                "probability": 0.2,
                "expected_return": -0.15,
                "description": "older bear path",
            },
        }
        return candidate

    def test_older_first_newer_second_yields_newer_current_state(self):
        session = MemorySession()
        first = run_cycle(
            session,
            as_of=self.OLDER,
            runner=ScriptedRunner(self._older_candidate()),
        )
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["thesis_count"], 1)
        self.assertEqual(first["stale_candidates"], 0)

        second = run_cycle(session, as_of=NOW, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        self.assertEqual(second["thesis_count"], 1)
        self.assertEqual(second["stale_candidates"], 0)
        # Every autonomous merge routes the canonical-key advisory lock
        # that closes the both-see-no-thesis create race.
        self.assertTrue(
            any("pg_advisory_xact_lock" in sql for sql, _params in session.calls)
        )

        (thesis_id,) = session.theses.keys()
        thesis = session.theses[thesis_id]
        # The newer cycle's claim is the current claim and the guard
        # advanced to its accepted reference.
        self.assertEqual(thesis["claim"], CANDIDATE["claim"])
        self.assertEqual(thesis["fusion_reference_at"], NOW)
        # The newer claim appended a version (2) and superseded the older
        # cycle's scenario legs (active legs are version 2).
        self.assertEqual(session.versions[thesis_id], 2)
        active_scenarios = [
            row for row in session.scenarios if row["superseded_at"] is None
        ]
        self.assertEqual(
            {row["name"] for row in active_scenarios}, {"bull", "base", "bear"}
        )
        self.assertTrue(
            all(row["version"] == 2 for row in active_scenarios),
            active_scenarios,
        )
        self.assertEqual(
            {row["expected_return"] for row in active_scenarios}, {0.1, 0.0, -0.2}
        )

    def test_newer_first_older_second_makes_the_older_job_a_complete_noop(self):
        session = MemorySession()
        newer = run_cycle(session, as_of=NOW, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(newer["status"], "completed")
        self.assertEqual(newer["thesis_count"], 1)

        older = run_cycle(
            session,
            as_of=self.OLDER,
            runner=ScriptedRunner(self._older_candidate()),
        )
        self.assertEqual(older["status"], "completed")
        self.assertEqual(older["error_count"], 0)
        # The stale cycle still serialized on the canonical-key advisory
        # lock before its lookup, then was rejected by the accepted-
        # reference guard: every child/current-state write was skipped.
        self.assertTrue(
            any("pg_advisory_xact_lock" in sql for sql, _params in session.calls)
        )
        self.assertEqual(older["stale_candidates"], 1)
        self.assertEqual(older["thesis_count"], 0)
        self.assertEqual(older["scenario_upserts"], 0)
        self.assertEqual(older["catalyst_upserts"], 0)
        self.assertEqual(older["playbook_upserts"], 0)
        self.assertEqual(older["watch_links"], 0)
        self.assertEqual(older["forecasts_frozen"], 0)
        self.assertEqual(older["opportunity_snapshots"], 0)
        self.assertEqual(older["falsification_runs"], 0)
        self.assertEqual(older["contradictions_attached"], 0)
        self.assertEqual(older["paused_count"], 0)
        self.assertEqual(older["group_count"], 0)

        (thesis_id,) = session.theses.keys()
        thesis = session.theses[thesis_id]
        # The newer cycle's claim/version/scenario state is untouched.
        self.assertEqual(thesis["claim"], CANDIDATE["claim"])
        self.assertEqual(thesis["fusion_reference_at"], NOW)
        self.assertEqual(session.versions[thesis_id], 1)
        active_scenarios = [
            row for row in session.scenarios if row["superseded_at"] is None
        ]
        self.assertEqual(len(active_scenarios), 3)
        self.assertTrue(all(row["version"] == 1 for row in active_scenarios))
        # No child state was appended: evidence links, catalysts, playbooks,
        # snapshots, groups, members, and falsification runs all stay at the
        # newer cycle's single set.
        self.assertEqual(len(session.evidence), 3)
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(len(session.playbooks), 1)
        self.assertEqual(len(session.snapshots), 1)
        self.assertEqual(len(session.groups), 1)
        self.assertEqual(len(session.members), 1)
        self.assertEqual(len(session.falsification_runs), 1)
        self.assertEqual(len(session.position_links), 0)


class ContextAffectedPriorityTests(unittest.TestCase):
    LINKED_ID = "35353535-3535-4535-8535-353535353535"
    AFFECTED_ID = "36363636-3636-4636-8636-363636363636"
    PLAIN_ID = "37373737-3737-4737-8737-373737373737"

    def _seed(self):
        session = MemorySession()
        # Plain thesis with the highest opportunity score.
        session.seed_thesis(
            self.PLAIN_ID,
            status="active",
            opportunity_score=0.9,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            self.PLAIN_ID,
            evidence_id="claim:plain-support",
            evidence_fingerprint="b" * 64,
        )
        # Affected thesis: recent context match, no position link.
        session.seed_thesis(
            self.AFFECTED_ID,
            status="active",
            opportunity_score=0.2,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            self.AFFECTED_ID,
            evidence_id="claim:affected-inv",
            relationship="invalidation",
            evidence_fingerprint="f" * 64,
        )
        playbook = session.seed_playbook(self.AFFECTED_ID)
        session.seed_context_match(
            playbook["id"], "99999999-9999-4999-8999-999999999999"
        )
        # Linked thesis (watch) with a low opportunity score.
        session.seed_thesis(
            self.LINKED_ID,
            status="active",
            opportunity_score=0.1,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            self.LINKED_ID,
            evidence_id="claim:linked-inv",
            relationship="invalidation",
            evidence_fingerprint="a" * 64,
        )
        session.position_links.append(
            {
                "position_id": "67676767-6767-4767-8767-676767676767",
                "thesis_id": self.LINKED_ID,
                "link_type": "watch",
                "created_at": NOW - timedelta(days=1),
                "removed_at": None,
            }
        )
        return session

    def test_affected_theses_rank_ahead_of_generic_high_opportunity(self):
        session = self._seed()
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["context_affected"], 1)
        self.assertEqual(result["second_pass_challenged"], 3)
        # Affected thesis is falsified/paused; the plain high-opportunity
        # thesis is challenged only after it and stays intact.
        self.assertEqual(session.theses[self.AFFECTED_ID]["status"], "paused")
        self.assertEqual(session.theses[self.PLAIN_ID]["status"], "active")
        affected_run = session.falsification_runs[
            (self.AFFECTED_ID, "autonomy:20260815T093000.000000")
        ]
        self.assertEqual(affected_run["status"], "falsified")
        plain_run = session.falsification_runs[
            (self.PLAIN_ID, "autonomy:20260815T093000.000000")
        ]
        self.assertEqual(plain_run["status"], "not_falsified")

    def test_linked_positions_still_sort_first_within_affected(self):
        session = self._seed()
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["second_pass_challenged"], 3)
        keys = [row[0] for row in session.falsification_runs]
        # The promoted thesis is challenged first; the second pass then runs
        # linked -> affected -> plain.
        promoted = [
            thesis_id
            for thesis_id in keys
            if thesis_id not in {self.LINKED_ID, self.AFFECTED_ID, self.PLAIN_ID}
        ]
        self.assertEqual(len(promoted), 1)
        self.assertEqual(keys[0], promoted[0])
        self.assertEqual(keys[1], self.LINKED_ID)
        self.assertEqual(keys[2], self.AFFECTED_ID)
        self.assertEqual(keys[3], self.PLAIN_ID)
        self.assertEqual(session.theses[self.LINKED_ID]["status"], "paused")
        self.assertEqual(session.theses[self.AFFECTED_ID]["status"], "paused")
        self.assertEqual(session.theses[self.PLAIN_ID]["status"], "active")

    def test_challenge_cap_is_shared_by_promotion_and_second_pass(self):
        session = self._seed()
        challenger = ScriptedChallenger()
        with patch.object(
            EvidenceRegistry,
            "collect",
            return_value=EvidenceCollection(items=tuple(evidence_items()), failures={}),
        ):
            result = run_autonomous_thesis_cycle(
                session,
                cycle_config(maximum_challenges_per_run=1),
                as_of=NOW,
                runner=ScriptedRunner(CANDIDATE),
                challenger=challenger,
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["promoted_count"], 1)
        self.assertEqual(result["challenge_limit"], 1)
        self.assertEqual(result["challenge_attempts"], 1)
        self.assertEqual(result["falsification_runs"], 1)
        self.assertEqual(result["second_pass_candidates"], 0)
        self.assertEqual(result["second_pass_challenged"], 0)
        self.assertEqual(challenger.calls, 1)


class PositionLinkTests(unittest.TestCase):
    def test_exact_normalized_symbol_match_links_watch(self):
        session = MemorySession()
        session.seed_holding("67676767-6767-4767-8767-676767676767", " ACME ")
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["watch_links"], 1)
        self.assertEqual(len(session.position_links), 1)
        link = session.position_links[0]
        self.assertEqual(link["link_type"], "watch")
        self.assertEqual(link["position_id"], "67676767-6767-4767-8767-676767676767")

    def test_exact_only_match_and_no_symbol_skip_linking(self):
        session = MemorySession()
        session.seed_holding("67676767-6767-4767-8767-676767676767", "ACMEX")
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["watch_links"], 0)
        self.assertEqual(session.position_links, [])

        candidate = copy.deepcopy(CANDIDATE)
        candidate["instrument"] = ""
        session2 = MemorySession()
        session2.seed_holding("67676767-6767-4767-8767-676767676767", "ACME")
        result2 = run_cycle(session2, runner=ScriptedRunner(candidate))
        self.assertEqual(result2["watch_links"], 0)
        self.assertEqual(session2.position_links, [])

    def test_rerun_does_not_duplicate_watch_links(self):
        session = MemorySession()
        session.seed_holding("67676767-6767-4767-8767-676767676767", "ACME")
        first = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        second = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(first["watch_links"], 1)
        self.assertEqual(second["watch_links"], 0)
        self.assertEqual(len(session.position_links), 1)

    def test_watch_linked_thesis_is_prioritized_in_second_pass(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, status="active", opportunity_score=0.1)
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:invalidate",
            relationship="invalidation",
            evidence_fingerprint="e" * 64,
        )
        session.position_links.append(
            {
                "position_id": "67676767-6767-4767-8767-676767676767",
                "thesis_id": EXISTING_ID,
                "link_type": "watch",
                "created_at": NOW - timedelta(days=1),
                "removed_at": None,
            }
        )
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")


class SecondPassReferenceSafetyTests(unittest.TestCase):
    """Second-pass reconstruction consumes only reference-visible state.

    The second falsification pass fails closed: theses whose current state
    is not provable at the reference (missing or post-reference
    created/updated timestamps) are never selected, challenged, or paused,
    and snapshot scenarios/evidence attachments are bounded by the same
    cutoff.  Durable live cycles (reference = acceptance time) retain the
    full falsification path.
    """

    PAST_ID = "38383838-3838-4838-8838-383838383838"
    FUTURE_ID = "39393939-3939-4939-8939-393939393939"
    MUTATED_ID = "3a3a3a3a-3a3a-4a3a-8a3a-3a3a3a3a3a"
    SCORED_ID = "3b3b3b3b-3b3b-4b3b-8b3b-3b3b3b3b3b"
    FUSED_ID = "3c3c3c3c-3c3c-4c3c-8c3c-3c3c3c3c3c"
    REPLAY_AT = NOW - timedelta(days=10)

    def _visible_thesis(self, thesis_id: str, opportunity: float) -> None:
        """A thesis whose whole state predates the replay reference."""
        session = self.session
        session.seed_thesis(
            thesis_id,
            status="active",
            opportunity_score=opportunity,
            last_evaluated_at=self.REPLAY_AT - timedelta(days=1),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        session.seed_evidence(
            thesis_id,
            evidence_id=f"claim:{thesis_id}-inv",
            relationship="invalidation",
            evidence_fingerprint="d" * 64,
            created_at=NOW - timedelta(days=30),
            source_timestamp=NOW - timedelta(days=30),
            available_at=NOW - timedelta(days=30),
        )

    def test_historical_replay_never_challenges_post_reference_theses(self):
        session = self.session = MemorySession()
        self._visible_thesis(self.PAST_ID, opportunity=0.9)
        # Created after the replay reference: its current state is future.
        session.seed_thesis(
            self.FUTURE_ID,
            status="active",
            opportunity_score=0.95,
            last_evaluated_at=NOW - timedelta(days=1),
            created_at=NOW - timedelta(days=5),
            updated_at=NOW - timedelta(days=5),
        )
        session.seed_evidence(
            self.FUTURE_ID,
            evidence_id="claim:future-inv",
            relationship="invalidation",
            evidence_fingerprint="e" * 64,
        )
        # Created before the reference but mutated after it: fail closed.
        session.seed_thesis(
            self.MUTATED_ID,
            status="active",
            opportunity_score=0.8,
            last_evaluated_at=NOW - timedelta(days=1),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=2),
        )
        result = run_cycle(
            session,
            as_of=self.REPLAY_AT,
            runner=ScriptedRunner(CANDIDATE),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["context_affected"], 0)
        # The promoted thesis is created during this run (post-reference) and
        # the two seeded post-reference theses are excluded: 3 total.
        self.assertEqual(result["second_pass_unversioned_excluded"], 3)
        # Only the reference-visible thesis is challenged, falsified, paused.
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(session.theses[self.PAST_ID]["status"], "paused")
        self.assertEqual(session.theses[self.FUTURE_ID]["status"], "active")
        self.assertEqual(session.theses[self.MUTATED_ID]["status"], "active")
        run_key = f"autonomy:{result['cycle_key']}"
        self.assertIn((self.PAST_ID, run_key), session.falsification_runs)
        self.assertNotIn((self.FUTURE_ID, run_key), session.falsification_runs)
        self.assertNotIn((self.MUTATED_ID, run_key), session.falsification_runs)

    def test_historical_replay_never_selects_newer_score_or_fusion_state(self):
        # Newer current scoring/fusion state cannot enter an older bounded
        # selection: a thesis whose opportunity score was written by an
        # evaluation after the reference (post-reference last_evaluated_at)
        # or which was claimed by a newer fusion cycle (post-reference
        # fusion_reference_at) is never challenged or paused from an older
        # verdict, no matter how high its current score ranks.
        session = self.session = MemorySession()
        self._visible_thesis(self.PAST_ID, opportunity=0.5)
        # Evaluated after the reference: its current score is future state.
        session.seed_thesis(
            self.SCORED_ID,
            status="active",
            opportunity_score=0.99,
            last_evaluated_at=NOW - timedelta(days=1),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        # Claimed by an autonomous fusion cycle after the reference: its
        # current content may reflect the newer claim.
        session.seed_thesis(
            self.FUSED_ID,
            status="active",
            opportunity_score=0.98,
            last_evaluated_at=self.REPLAY_AT - timedelta(days=1),
            fusion_reference_at=NOW - timedelta(days=2),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        selected = _second_pass_candidates(
            session,
            limit=25,
            excluded_ids=[],
            reference=self.REPLAY_AT,
            context_since=self.REPLAY_AT - timedelta(days=7),
        )
        # Only the fully reference-visible thesis is selected, and each
        # selected row carries the optimistic pause tokens.
        self.assertEqual([str(row["id"]) for row in selected], [self.PAST_ID])
        self.assertEqual(selected[0]["status"], "active")
        self.assertIsNotNone(selected[0]["updated_at"])
        self.assertIsNotNone(selected[0]["last_evaluated_at"])
        self.assertIsNone(selected[0]["fusion_reference_at"])
        result = run_cycle(
            session,
            as_of=self.REPLAY_AT,
            runner=ScriptedRunner(CANDIDATE),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(result["second_pass_stale_skipped"], 0)
        # The promoted thesis plus both post-reference-state theses are
        # excluded: 3 total.
        self.assertEqual(result["second_pass_unversioned_excluded"], 3)
        self.assertEqual(session.theses[self.PAST_ID]["status"], "paused")
        self.assertEqual(session.theses[self.SCORED_ID]["status"], "active")
        self.assertEqual(session.theses[self.FUSED_ID]["status"], "active")
        run_key = f"autonomy:{result['cycle_key']}"
        self.assertIn((self.PAST_ID, run_key), session.falsification_runs)
        self.assertNotIn((self.SCORED_ID, run_key), session.falsification_runs)
        self.assertNotIn((self.FUSED_ID, run_key), session.falsification_runs)

    def test_historical_replay_bounds_matches_scenarios_and_attachments(self):
        session = self.session = MemorySession()
        self._visible_thesis(self.PAST_ID, opportunity=0.2)
        playbook = session.seed_playbook(
            self.PAST_ID, created_at=NOW - timedelta(days=30)
        )
        # Context match observed within the recent window and created well
        # before the reference: reference-visible and context-affecting.
        session.seed_context_match(
            playbook["id"],
            "99999999-9999-4999-8999-999999999999",
            observed_at=self.REPLAY_AT - timedelta(days=2),
            created_at=NOW - timedelta(days=30),
        )
        # Same thesis, second match whose row was created after the
        # reference: must not count as context for the replay.
        session.seed_context_match(
            playbook["id"],
            "99999999-9999-4999-8999-888888888888",
            observed_at=self.REPLAY_AT - timedelta(days=1),
            created_at=NOW - timedelta(days=5),
        )
        # Snapshot attachments: one leg and one evidence row visible at the
        # reference, one of each created after it.
        session.scenarios.append(
            {
                "id": _id(f"scenario:{self.PAST_ID}:visible"),
                "thesis_id": self.PAST_ID,
                "name": "visible-leg",
                "probability": 0.5,
                "expected_return": 0.0,
                "is_base_case": True,
                "version": 1,
                "superseded_at": None,
                "created_at": NOW - timedelta(days=30),
            }
        )
        session.scenarios.append(
            {
                "id": _id(f"scenario:{self.PAST_ID}:future"),
                "thesis_id": self.PAST_ID,
                "name": "future-leg",
                "probability": 0.5,
                "expected_return": 0.1,
                "is_base_case": False,
                "version": 1,
                "superseded_at": None,
                "created_at": NOW - timedelta(days=5),
            }
        )
        session.seed_evidence(
            self.PAST_ID,
            evidence_id="claim:past-visible",
            relationship="supports",
            evidence_fingerprint="a" * 64,
            created_at=NOW - timedelta(days=30),
            source_timestamp=NOW - timedelta(days=30),
            available_at=NOW - timedelta(days=30),
        )
        session.seed_evidence(
            self.PAST_ID,
            evidence_id="claim:past-future",
            relationship="supports",
            evidence_fingerprint="b" * 64,
            created_at=NOW - timedelta(days=5),
            source_timestamp=NOW - timedelta(days=5),
            available_at=NOW - timedelta(days=5),
        )
        challenger = RecordingChallenger()
        result = run_cycle(
            session,
            as_of=self.REPLAY_AT,
            runner=ScriptedRunner(CANDIDATE),
            challenger=challenger,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["second_pass_candidates"], 1)
        # Only the reference-visible context match counts.
        self.assertEqual(result["context_affected"], 1)
        snapshot, evidence = next(
            pair for pair in challenger.captured if pair[0].thesis_id == self.PAST_ID
        )
        # Only the reference-visible scenario leg enters the snapshot.
        self.assertEqual(
            [scenario.label for scenario in snapshot.scenarios],
            ["visible-leg"],
        )
        # The post-reference attachment never reaches the challenger; the
        # reference-visible attachment does.
        evidence_ids = {signal.evidence_id for signal in evidence if signal.evidence_id}
        self.assertIn("claim:past-visible", evidence_ids)
        self.assertNotIn("claim:past-future", evidence_ids)

    def test_live_cycle_second_pass_keeps_full_falsification(self):
        # A durable live job (reference = acceptance time) still challenges
        # and pauses reference-visible high-opportunity theses exactly as
        # before: prioritization, challenge isolation, and bounds unchanged.
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            status="active",
            opportunity_score=0.9,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:invalidate",
            relationship="invalidation",
            evidence_fingerprint="d" * 64,
        )
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["second_pass_unversioned_excluded"], 0)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")
        run = session.falsification_runs[
            (EXISTING_ID, "autonomy:20260815T093000.000000")
        ]
        self.assertEqual(run["status"], "falsified")

    def test_live_cycle_fails_closed_on_missing_or_future_timestamps(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            status="active",
            opportunity_score=0.9,
            created_at=None,
            updated_at=None,
        )
        session.seed_thesis(
            self.MUTATED_ID,
            status="active",
            opportunity_score=0.8,
            created_at=NOW - timedelta(days=5),
            updated_at=NOW + timedelta(hours=1),
        )
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        # Missing lifecycle timestamps cannot prove visibility: excluded.
        self.assertEqual(result["second_pass_candidates"], 0)
        self.assertEqual(result["second_pass_challenged"], 0)
        self.assertEqual(result["second_pass_unversioned_excluded"], 2)
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "active")
        self.assertEqual(session.theses[self.MUTATED_ID]["status"], "active")
        run_key = f"autonomy:{result['cycle_key']}"
        self.assertNotIn((EXISTING_ID, run_key), session.falsification_runs)
        self.assertNotIn((self.MUTATED_ID, run_key), session.falsification_runs)

    def test_second_pass_helpers_require_an_explicit_reference(self):
        # A historical path can never invoke the second-pass helpers without
        # making the reference-safety decision: the cutoff is a required
        # keyword argument, so an accidental unversioned call fails closed.
        session = MemorySession()
        with self.assertRaises(TypeError):
            _second_pass_candidates(
                session,
                limit=5,
                excluded_ids=[],
                context_since=NOW - timedelta(days=7),
            )
        with self.assertRaises(TypeError):
            _load_second_pass_snapshot(
                session,
                {
                    "id": EXISTING_ID,
                    "claim": "Existing claim",
                    "direction": "long",
                    "status": "active",
                    "invalidation_conditions": [],
                    "opportunity_score": 0.0,
                    "last_evaluated_at": None,
                },
                cost=0.0,
                cycle_key="20260815T093000.000000",
            )
        with self.assertRaises(TypeError):
            _count_unversioned_second_pass_candidates(session)
        # With the explicit decision both helpers behave deterministically.
        self.assertEqual(
            _second_pass_candidates(
                session,
                limit=5,
                excluded_ids=[],
                reference=NOW,
                context_since=NOW - timedelta(days=7),
            ),
            [],
        )
        self.assertEqual(
            _count_unversioned_second_pass_candidates(session, reference=NOW),
            0,
        )


class RacingSession(MemorySession):
    """MemorySession that mutates one thesis exactly between second-pass
    selection and the conditional pause UPDATE, simulating a concurrent
    or newer cycle landing during model latency.

    ``mutate`` is called with the thesis row immediately before the pause
    executes (identified by the conditional UPDATE's ``updated_at`` token
    parameter); the first-pass unconditional pause never carries that
    parameter and is not intercepted.
    """

    def __init__(self, mutate=None):
        super().__init__()
        self._mutate = mutate

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        if (
            "SET status = 'paused'" in sql
            and params is not None
            and "updated_at" in params
        ):
            row = self.theses.get(str(params["id"]))
            if row is not None and self._mutate is not None:
                self._mutate(row)
        return super().execute(statement, params)


class SecondPassPauseRaceTests(unittest.TestCase):
    """The second-pass pause is an optimistic conditional write.

    The challenge verdict is computed against state visible at the cycle
    reference, but the pause executes after model latency.  A thesis
    re-evaluated, re-fused, or re-stated by a concurrent/newer cycle in
    that window must never be paused from the older verdict (the
    reference-bounded falsification audit still persists); unchanged
    breached theses still pause exactly once.
    """

    def _breached_seed(self, session) -> None:
        session.seed_thesis(
            EXISTING_ID,
            status="active",
            opportunity_score=0.9,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:invalidate",
            relationship="invalidation",
            evidence_fingerprint="d" * 64,
        )

    def _run_with_racer(self, mutate):
        session = RacingSession(mutate=mutate)
        self._breached_seed(session)
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        return session, result

    def test_concurrent_evaluation_between_selection_and_pause_is_a_noop(self):
        # A newer cycle re-evaluates the thesis after selection: the older
        # verdict must not pause the newer score state.
        session, result = self._run_with_racer(
            lambda row: row.update(last_evaluated_at=NOW + timedelta(seconds=5))
        )
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(result["second_pass_stale_skipped"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "active")
        # The reference-bounded challenge/falsification audit still persists.
        run = session.falsification_runs[
            (EXISTING_ID, "autonomy:20260815T093000.000000")
        ]
        self.assertEqual(run["status"], "falsified")

    def test_concurrent_fusion_claim_between_selection_and_pause_is_a_noop(self):
        # A newer cycle claims/fuses the thesis after the reference:
        # fusion_reference_at moves past it, so the older verdict cannot
        # pause the newer content.
        session, result = self._run_with_racer(
            lambda row: row.update(fusion_reference_at=NOW + timedelta(minutes=1))
        )
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(result["second_pass_stale_skipped"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "active")
        self.assertIn(
            (EXISTING_ID, "autonomy:20260815T093000.000000"), session.falsification_runs
        )

    def test_concurrent_status_change_between_selection_and_pause_is_a_noop(self):
        # A newer cycle already moved the thesis (here: paused it): the
        # status token no longer matches, so nothing is paused twice.
        session, result = self._run_with_racer(lambda row: row.update(status="paused"))
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(result["second_pass_stale_skipped"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")

    def test_concurrent_row_mutation_between_selection_and_pause_is_a_noop(self):
        # Any other row mutation bumps updated_at past the selected token.
        session, result = self._run_with_racer(
            lambda row: row.update(updated_at=NOW + timedelta(seconds=1))
        )
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(result["second_pass_stale_skipped"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "active")

    def test_unchanged_breached_thesis_still_pauses_exactly_once(self):
        # No concurrent change: the optimistic UPDATE applies, stamps
        # updated_at, and the run reports exactly one paused thesis.
        session, result = self._run_with_racer(mutate=None)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(result["second_pass_stale_skipped"], 0)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")
        self.assertEqual(session.theses[EXISTING_ID]["updated_at"], NOW)

    def test_same_cycle_contradiction_recompute_still_pauses(self):
        # The second pass itself recomputes scores at the reference when
        # contradictions attach, moving last_evaluated_at exactly to the
        # reference; that same-cycle write stays safe for the pause.
        session = MemorySession()
        self._breached_seed(session)
        proposal = {
            "kind": "counter_evidence",
            "statement": "The cited evidence supports the opposite view",
            "citations": ["source_claim:claim:0005"],
        }
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=ScriptedChallenger(proposal=proposal),
        )
        self.assertEqual(result["status"], "completed")
        # Promoted thesis and second-pass thesis each attach the citation.
        self.assertEqual(result["contradictions_attached"], 2)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(result["second_pass_stale_skipped"], 0)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")
        self.assertEqual(session.theses[EXISTING_ID]["last_evaluated_at"], NOW)


class PlaybookTests(unittest.TestCase):
    def test_playbook_is_built_and_persisted_idempotently(self):
        session = MemorySession()
        first = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(first["playbook_upserts"], 1)
        self.assertEqual(len(session.playbooks), 1)
        active = [row for row in session.playbooks if row["superseded_at"] is None]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["version"], 1)

        second = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(second["playbook_upserts"], 0)
        self.assertEqual(len(session.playbooks), 1)


class ForecastTests(unittest.TestCase):
    def test_missing_forecasts_backfill_after_market_price_arrives(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            company="Acme Corporation",
            symbol="ACME",
            input_fingerprint="f" * 64,
        )
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        for label, expected_return in (("bull", 0.1), ("base", 0.0), ("bear", -0.2)):
            session.scenarios.append(
                {
                    "id": _id(f"scenario:{label}"),
                    "thesis_id": EXISTING_ID,
                    "name": label,
                    "expected_return": expected_return,
                    "superseded_at": None,
                }
            )

        first = _backfill_missing_forecasts(session, NOW)
        second = _backfill_missing_forecasts(session, NOW)

        self.assertEqual(first, 3)
        self.assertEqual(second, 0)
        self.assertEqual(len(session.forecasts), 3)

    def test_forecasts_are_frozen_with_deterministic_targets(self):
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["forecasts_frozen"], 3)
        thesis_id = next(iter(session.theses))
        by_label = {}
        for row in session.forecasts:
            self.assertIsNone(row["superseded_at"])
            self.assertEqual(row["thesis_id"], thesis_id)
            self.assertEqual(row["forecast_type"], "price")
            self.assertEqual(row["as_of"], NOW)
            label = row["forecast_key"].split(":")[2]
            by_label[label] = row
        # Long thesis: target = close * (1 + fractional P&L).
        self.assertEqual(by_label["bull"]["direction"], "up")
        self.assertEqual(by_label["bull"]["target_value"], 110.0)
        self.assertEqual(by_label["base"]["direction"], "flat")
        self.assertEqual(by_label["base"]["target_value"], 100.0)
        self.assertEqual(by_label["bear"]["direction"], "down")
        self.assertEqual(by_label["bear"]["target_value"], 80.0)
        expected_date = NOW.date() + timedelta(days=90)
        for row in session.forecasts:
            self.assertEqual(row["target_date"], expected_date)
            self.assertTrue(row["forecast_key"].startswith(f"autonomy:{thesis_id}:"))
            scenario = next(
                s for s in session.scenarios if s["id"] == row["scenario_id"]
            )
            self.assertEqual(scenario["thesis_id"], thesis_id)

    def test_short_thesis_uses_the_inverse_factor(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["direction"] = "short"
        candidate["scenarios"] = {
            "bull": {
                "probability": 0.3,
                "expected_return": 0.2,
                "description": "competition fails and margins widen",
            },
            "base": {
                "probability": 0.5,
                "expected_return": 0.0,
                "description": "competition holds margins flat",
            },
            "bear": {
                "probability": 0.2,
                "expected_return": -0.2,
                "description": "competition erodes margins",
            },
        }
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result = run_cycle(session, runner=ScriptedRunner(candidate))
        self.assertEqual(result["forecasts_frozen"], 3)
        by_label = {}
        for row in session.forecasts:
            by_label[row["forecast_key"].split(":")[2]] = row
        # Short thesis: target = close * (1 - fractional P&L); a bull leg is
        # a falling price.
        self.assertEqual(by_label["bull"]["direction"], "down")
        self.assertEqual(by_label["bull"]["target_value"], 80.0)
        self.assertEqual(by_label["base"]["direction"], "flat")
        self.assertEqual(by_label["base"]["target_value"], 100.0)
        self.assertEqual(by_label["bear"]["direction"], "up")
        self.assertEqual(by_label["bear"]["target_value"], 120.0)

    def test_neutral_thesis_and_invalid_extremes_skip_freezing(self):
        neutral = copy.deepcopy(CANDIDATE)
        neutral["direction"] = "neutral"
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result = run_cycle(session, runner=ScriptedRunner(neutral))
        self.assertEqual(result["forecasts_frozen"], 0)
        self.assertEqual(session.forecasts, [])

        # Long thesis with a fractional return at or below -1 has no
        # positive target; it stays unknown and is never clamped.
        extreme = copy.deepcopy(CANDIDATE)
        extreme["scenarios"]["bull"]["expected_return"] = -1.5
        extreme["scenarios"]["base"]["expected_return"] = -1.0
        extreme["scenarios"]["bear"]["expected_return"] = -0.2
        session2 = MemorySession()
        session2.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result2 = run_cycle(session2, runner=ScriptedRunner(extreme))
        self.assertEqual(result2["forecasts_frozen"], 1)
        self.assertEqual(len(session2.forecasts), 1)
        self.assertEqual(session2.forecasts[0]["target_value"], 80.0)

        # Short thesis with a return above +1 has a non-positive factor.
        short_extreme = copy.deepcopy(CANDIDATE)
        short_extreme["direction"] = "short"
        short_extreme["scenarios"]["bear"]["expected_return"] = 1.5
        session3 = MemorySession()
        session3.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result3 = run_cycle(session3, runner=ScriptedRunner(short_extreme))
        self.assertEqual(result3["forecasts_frozen"], 2)

    def test_no_symbol_or_close_skips_freezing(self):
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["forecasts_frozen"], 0)
        self.assertEqual(session.forecasts, [])

    def test_matured_forecasts_resolve_once_from_point_in_time_prices(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        target_day = NOW.date() - timedelta(days=1)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)
        session.market_data["ACME"] = [
            (boundary - timedelta(hours=4), 110.0),
            (boundary - timedelta(hours=8), 100.0),
        ]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["hit"], 1)
        self.assertEqual(counts["miss"], 0)
        self.assertEqual(counts["inconclusive"], 0)
        self.assertEqual(len(session.outcomes), 1)
        # The fake retains the measured values for point-in-time assertions.
        outcome = session.outcomes["44444444-4444-4444-8444-444444444444"]
        self.assertEqual(outcome["status"], "hit")
        self.assertEqual(outcome["actual_value"], 110.0)
        self.assertEqual(outcome["measured_at"], NOW)
        # Outcomes are recorded once and never overwritten.
        again = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(again["hit"], 0)
        self.assertEqual(len(session.outcomes), 1)

    def test_miss_inconclusive_and_open_outcomes(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        target_day = NOW.date() - timedelta(days=1)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)
        session.market_data["ACME"] = [(boundary - timedelta(hours=4), 90.0)]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["miss"], 1)

        session2 = MemorySession()
        session2.seed_thesis(EXISTING_ID, symbol="NOPRICE")
        session2.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=NOW.date() - timedelta(days=30),
        )
        counts = _resolve_matured_forecasts(session2, NOW)
        self.assertEqual(counts["inconclusive"], 1)

        session3 = MemorySession()
        session3.seed_thesis(EXISTING_ID, symbol="NOPRICE")
        session3.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=NOW.date() - timedelta(days=1),
        )
        counts = _resolve_matured_forecasts(session3, NOW)
        self.assertEqual(counts["open"], 1)
        self.assertEqual(session3.outcomes, {})

    def test_run_before_target_boundary_never_resolves(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 110.0)]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=NOW.date(),  # boundary (end of today UTC) not reached
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session.outcomes, {})

    def test_resolution_uses_the_target_boundary_close_not_later_bars(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        target_day = NOW.date() - timedelta(days=1)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)
        session.market_data["ACME"] = [
            (boundary - timedelta(hours=4), 110.0),  # terminal close at boundary
            (boundary + timedelta(hours=2), 95.0),  # post-boundary bar: never used
            (NOW + timedelta(days=2), 80.0),  # much later bar: never used
        ]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["hit"], 1)
        self.assertEqual(counts["miss"], 0)
        outcome = session.outcomes["44444444-4444-4444-8444-444444444444"]
        self.assertEqual(outcome["actual_value"], 110.0)
        self.assertEqual(outcome["measured_at"], NOW)
        # A delayed run (days later) still measures the same boundary close
        # and never records a second outcome.
        delayed = _resolve_matured_forecasts(session, NOW + timedelta(days=5))
        self.assertEqual(delayed["hit"], 0)
        self.assertEqual(delayed["miss"], 0)
        self.assertEqual(len(session.outcomes), 1)
        self.assertEqual(
            session.outcomes["44444444-4444-4444-8444-444444444444"]["actual_value"],
            110.0,
        )

    def test_weekend_target_uses_the_prior_available_close(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        saturday = date(2026, 8, 8)  # a Saturday
        session.market_data["ACME"] = [
            (datetime(2026, 8, 7, 21, 0, tzinfo=UTC), 100.0),  # Friday close
        ]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=saturday,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["miss"], 1)  # 100 < 105 on an up forecast
        self.assertEqual(
            session.outcomes["44444444-4444-4444-8444-444444444444"]["actual_value"],
            100.0,
        )

    def test_bars_unavailable_at_replay_time_are_excluded(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        target_day = NOW.date() - timedelta(days=30)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)
        # Timestamped at/before the boundary, but only ingested AFTER the
        # replay cutoff: a replay run must not see it.
        session.market_data["ACME"] = [
            (boundary - timedelta(hours=4), 110.0, NOW + timedelta(minutes=5))
        ]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["inconclusive"], 1)
        self.assertEqual(
            session.outcomes["44444444-4444-4444-8444-444444444444"]["status"],
            "inconclusive",
        )

        # Control: the same bar ingested before the cutoff is eligible.
        session2 = MemorySession()
        session2.seed_thesis(EXISTING_ID, symbol="ACME")
        session2.market_data["ACME"] = [
            (boundary - timedelta(hours=4), 110.0, NOW - timedelta(days=31))
        ]
        session2.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session2, NOW)
        self.assertEqual(counts["hit"], 1)

    def test_bars_revised_after_replay_time_are_excluded(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        target_day = NOW.date() - timedelta(days=30)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)
        # Ingested before the replay cutoff but REVISED after it (any row
        # mutation bumps updated_at): a replay run must not see the bar
        # even though its created_at predates the cutoff.
        session.market_data["ACME"] = [
            (
                boundary - timedelta(hours=4),
                110.0,
                NOW - timedelta(days=31),
                NOW + timedelta(minutes=5),
            )
        ]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["inconclusive"], 1)
        self.assertEqual(
            session.outcomes["44444444-4444-4444-8444-444444444444"]["status"],
            "inconclusive",
        )

        # Control: ingested and last revised before the cutoff stays
        # eligible and resolves exactly like the created_at-only control.
        session2 = MemorySession()
        session2.seed_thesis(EXISTING_ID, symbol="ACME")
        session2.market_data["ACME"] = [
            (
                boundary - timedelta(hours=4),
                110.0,
                NOW - timedelta(days=31),
                NOW - timedelta(days=31),
            )
        ]
        session2.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session2, NOW)
        self.assertEqual(counts["hit"], 1)

    def test_resolver_owns_price_forecasts_only(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        boundary = datetime.combine(
            NOW.date() - timedelta(days=1), time.max, tzinfo=UTC
        )
        session.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            forecast_type="earnings",
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session.outcomes, {})

        # A matured price forecast is resolved alongside the non-price one,
        # which stays open for its domain-specific resolver.
        session.seed_forecast(
            "55555555-5555-4555-8555-555555555555",
            direction="up",
            target_value=105.0,
            forecast_type="price",
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["hit"], 1)
        self.assertEqual(
            set(session.outcomes), {"55555555-5555-4555-8555-555555555555"}
        )

    def test_resolver_excludes_forecasts_created_or_frozen_after_reference(self):
        # A historical replay must see exactly the forecasts that existed at
        # its cutoff: a forecast persisted (created_at) or frozen (as_of)
        # after the reference is invisible, even if it is current today.
        reference = NOW - timedelta(days=2)
        target_day = reference.date() - timedelta(days=1)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)

        # Created after the reference (but before today): excluded.
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        session.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            as_of=reference - timedelta(days=1),
            created_at=reference + timedelta(hours=1),
        )
        counts = _resolve_matured_forecasts(session, reference)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session.outcomes, {})

        # Frozen (as_of) after the reference: excluded the same way.
        session.seed_forecast(
            "55555555-5555-4555-8555-555555555555",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            as_of=reference + timedelta(hours=1),
            created_at=reference - timedelta(days=1),
        )
        counts = _resolve_matured_forecasts(session, reference)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session.outcomes, {})

        # Control: the same forecasts visible at the reference resolve once.
        session2 = MemorySession()
        session2.seed_thesis(EXISTING_ID, symbol="ACME")
        session2.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session2.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            as_of=reference - timedelta(days=1),
            created_at=reference - timedelta(days=1),
        )
        session2.seed_forecast(
            "55555555-5555-4555-8555-555555555555",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            as_of=reference - timedelta(days=1),
            created_at=reference - timedelta(days=1),
        )
        counts = _resolve_matured_forecasts(session2, reference)
        self.assertEqual(counts["hit"], 2)
        self.assertEqual(len(session2.outcomes), 2)

    def test_resolver_treats_superseded_at_point_in_time(self):
        # A forecast superseded AFTER the reference was still active at the
        # reference and must resolve; superseded on/before the reference it
        # was already inactive and must not.
        reference = NOW - timedelta(days=2)
        target_day = reference.date() - timedelta(days=1)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)

        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        session.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            superseded_at=reference + timedelta(days=1),  # active at reference
        )
        counts = _resolve_matured_forecasts(session, reference)
        self.assertEqual(counts["hit"], 1)
        self.assertEqual(
            session.outcomes["44444444-4444-4444-8444-444444444444"]["status"],
            "hit",
        )

        # Superseded exactly at the reference: no longer active.
        session2 = MemorySession()
        session2.seed_thesis(EXISTING_ID, symbol="ACME")
        session2.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session2.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            superseded_at=reference,
        )
        counts = _resolve_matured_forecasts(session2, reference)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session2.outcomes, {})

        # Superseded before the reference: excluded as well.
        session3 = MemorySession()
        session3.seed_thesis(EXISTING_ID, symbol="ACME")
        session3.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session3.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            superseded_at=reference - timedelta(days=1),
        )
        counts = _resolve_matured_forecasts(session3, reference)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session3.outcomes, {})

        # A non-price forecast is untouched by all of this: even superseded
        # after the reference, it stays open for its domain-specific
        # resolver and records nothing here.
        session.seed_forecast(
            "55555555-5555-4555-8555-555555555555",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            forecast_type="earnings",
            superseded_at=reference + timedelta(days=1),
        )
        counts = _resolve_matured_forecasts(session, reference)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(
            set(session.outcomes), {"44444444-4444-4444-8444-444444444444"}
        )

    def test_rerun_with_later_as_of_keeps_one_active_forecast_per_scenario(self):
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        first = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(first["forecasts_frozen"], 3)
        self.assertEqual(len(session.forecasts), 3)
        # A later rerun over the same scenarios must not freeze a second
        # active forecast: the first frozen as_of/close/target/date wins.
        second = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            as_of=NOW + timedelta(days=1),
        )
        self.assertEqual(second["forecasts_frozen"], 0)
        active = [row for row in session.forecasts if row["superseded_at"] is None]
        self.assertEqual(len(active), 3)
        self.assertEqual(len(session.forecasts), 3)
        for row in active:
            self.assertEqual(row["as_of"], NOW)

    def test_mixed_case_stored_symbol_still_freezes_forecasts(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            symbol=" acme ",
            direction="long",
            horizon="months",
        )
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        for label, expected_return in (("bull", 0.1), ("base", 0.0), ("bear", -0.2)):
            session.scenarios.append(
                {
                    "id": _id(f"scenario:{label}"),
                    "thesis_id": EXISTING_ID,
                    "name": label,
                    "expected_return": expected_return,
                    "superseded_at": None,
                }
            )
        frozen = _backfill_missing_forecasts(session, NOW)
        self.assertEqual(frozen, 3)
        by_direction = {
            row["direction"]: row["target_value"] for row in session.forecasts
        }
        self.assertEqual(by_direction, {"up": 110.0, "flat": 100.0, "down": 80.0})

    def test_promoted_dotted_lowercase_symbol_is_canonicalized(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["subject"] = "Berkshire Hathaway"
        candidate["instrument"] = "BRK.B"
        candidate["claim"] = "Berkshire Hathaway insurance float should compound"
        entities = [
            NormalizedEntity.create(
                "company",
                "berkshire-hathaway",
                "Berkshire Hathaway",
            ),
            # Mixed-case dotted display name: persisted canonical.
            NormalizedEntity.create("symbol", "brk-b", "Brk.B"),
        ]
        items = [evidence_item(index, entities=entities) for index in range(3)]
        session = MemorySession()
        session.market_data["BRK.B"] = [(NOW - timedelta(hours=1), 100.0)]
        with patch.object(
            EvidenceRegistry,
            "collect",
            return_value=EvidenceCollection(items=tuple(items), failures={}),
        ):
            result = run_autonomous_thesis_cycle(
                session,
                cycle_config(),
                as_of=NOW,
                runner=ScriptedRunner(candidate),
                challenger=ScriptedChallenger(),
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["forecasts_frozen"], 3)
        thesis = next(iter(session.theses.values()))
        self.assertEqual(thesis["symbol"], "BRK.B")
        self.assertEqual(thesis["company"], "Berkshire Hathaway")
        for row in session.forecasts:
            self.assertIsNone(row["superseded_at"])
            self.assertEqual(row["as_of"], NOW)


class ForecastBackfillCutoffTests(unittest.TestCase):
    """Backfill consumes only thesis/scenario state visible at the reference.

    A historical or delayed run must never backdate a forecast for thesis
    or scenario state that did not exist at its accepted reference, while
    point-in-time visible legacy scenarios still backfill once prices
    arrive and live idempotency and bounds stay intact.
    """

    def _seed(self, session, **thesis_overrides) -> MemorySession:
        session.seed_thesis(
            EXISTING_ID,
            company="Acme Corporation",
            symbol="ACME",
            input_fingerprint="f" * 64,
            **thesis_overrides,
        )
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        for label, expected_return in (("bull", 0.1), ("base", 0.0), ("bear", -0.2)):
            session.scenarios.append(
                {
                    "id": _id(f"scenario:{label}"),
                    "thesis_id": EXISTING_ID,
                    "name": label,
                    "expected_return": expected_return,
                    "created_at": NOW - timedelta(days=2),
                    "superseded_at": None,
                }
            )
        return session

    def test_thesis_created_after_cutoff_is_not_backfilled(self):
        session = self._seed(MemorySession(), created_at=NOW + timedelta(hours=1))
        self.assertEqual(_backfill_missing_forecasts(session, NOW), 0)
        self.assertEqual(session.forecasts, [])

    def test_thesis_updated_after_cutoff_is_not_backfilled(self):
        session = self._seed(MemorySession(), updated_at=NOW + timedelta(hours=1))
        self.assertEqual(_backfill_missing_forecasts(session, NOW), 0)
        self.assertEqual(session.forecasts, [])

    def test_fusion_reference_after_cutoff_is_not_backfilled(self):
        # A thesis whose accepted fusion reference postdates the cutoff was
        # not accepted-fusion content at the reference: no forecast.
        session = self._seed(
            MemorySession(), fusion_reference_at=NOW + timedelta(hours=1)
        )
        self.assertEqual(_backfill_missing_forecasts(session, NOW), 0)
        self.assertEqual(session.forecasts, [])

    def test_scenario_created_after_cutoff_is_not_backfilled(self):
        session = self._seed(MemorySession())
        late = session.scenarios[2]
        late["created_at"] = NOW + timedelta(hours=1)
        frozen = _backfill_missing_forecasts(session, NOW)
        self.assertEqual(frozen, 2)
        self.assertFalse(
            any(row["scenario_id"] == late["id"] for row in session.forecasts)
        )
        visible = session.scenarios[:2]
        self.assertEqual(
            {row["scenario_id"] for row in session.forecasts},
            {s["id"] for s in visible},
        )

    def test_scenario_superseded_after_cutoff_is_still_visible(self):
        # A legacy scenario only superseded later is point-in-time visible
        # at the reference and backfills once price data arrives.
        session = self._seed(MemorySession())
        for scenario in session.scenarios:
            scenario["superseded_at"] = NOW + timedelta(days=1)
        self.assertEqual(_backfill_missing_forecasts(session, NOW), 3)
        self.assertEqual(len(session.forecasts), 3)

    def test_scenario_superseded_on_or_before_cutoff_is_excluded(self):
        session = self._seed(MemorySession())
        session.scenarios[0]["superseded_at"] = NOW - timedelta(days=1)
        session.scenarios[1]["superseded_at"] = NOW  # exactly on the cutoff
        frozen = _backfill_missing_forecasts(session, NOW)
        self.assertEqual(frozen, 1)
        active = [s for s in session.scenarios if s["superseded_at"] is None]
        self.assertEqual(
            [row["scenario_id"] for row in session.forecasts], [active[0]["id"]]
        )

    def test_current_visible_row_still_backfills_and_stays_idempotent(self):
        # A fully visible current row (fusion reference in the past or NULL,
        # created/updated before the cutoff, active scenarios) still
        # backfills exactly once; a rerun stays a no-op.
        session = self._seed(
            MemorySession(), fusion_reference_at=NOW - timedelta(days=1)
        )
        first = _backfill_missing_forecasts(session, NOW)
        second = _backfill_missing_forecasts(session, NOW)
        self.assertEqual(first, 3)
        self.assertEqual(second, 0)
        self.assertEqual(len(session.forecasts), 3)

    def test_forecast_active_at_cutoff_then_superseded_blocks_replay(self):
        # A forecast frozen before the reference and only superseded after
        # it was ACTIVE at the reference: a later replay must not re-freeze
        # the scenario (the original run at the reference saw the forecast
        # and froze nothing), even though the row is no longer active
        # today.  The other scenarios are the visible control and still
        # backfill exactly once.
        reference = NOW - timedelta(days=2)
        session = self._seed(
            MemorySession(),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        session.market_data["ACME"] = [(reference - timedelta(hours=1), 100.0)]
        session.seed_forecast(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            scenario_id=_id("scenario:bull"),
            as_of=reference - timedelta(days=1),
            superseded_at=reference + timedelta(days=1),
        )
        seeded_ids = {row["id"] for row in session.forecasts}
        frozen = _backfill_missing_forecasts(session, reference)
        appended = [row for row in session.forecasts if row["id"] not in seeded_ids]
        self.assertEqual(frozen, 2)
        # Only base/bear were frozen by the replay; no duplicate bull row.
        self.assertEqual(
            {row["scenario_id"] for row in appended},
            {_id("scenario:base"), _id("scenario:bear")},
        )
        # The superseded bull forecast stays exactly as seeded (immutable
        # history, never re-frozen at the reference).
        self.assertEqual(
            [
                row["id"]
                for row in session.forecasts
                if row["scenario_id"] == _id("scenario:bull")
            ],
            ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"],
        )

    def test_forecast_superseded_on_or_before_cutoff_does_not_block_replay(self):
        # A forecast already superseded on/before the reference was not
        # active at it; the scenario legitimately has no forecast at the
        # reference and backfills once (same point-in-time boundary as the
        # scenario supersede guard).
        reference = NOW - timedelta(days=2)
        session = self._seed(
            MemorySession(),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        session.market_data["ACME"] = [(reference - timedelta(hours=1), 100.0)]
        session.seed_forecast(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            scenario_id=_id("scenario:bull"),
            as_of=reference - timedelta(days=1),
            superseded_at=reference - timedelta(days=1),
        )
        seeded = len(session.forecasts)
        self.assertEqual(_backfill_missing_forecasts(session, reference), 3)
        # Exactly the three new forecast rows were appended; the seeded
        # historical row is untouched.
        self.assertEqual(len(session.forecasts) - seeded, 3)

    def test_forecast_frozen_after_cutoff_does_not_block_replay(self):
        # A forecast frozen after the reference did not exist at it (even
        # though it is superseded today), so the scenario backfills exactly
        # as the original run at the reference would have.
        reference = NOW - timedelta(days=2)
        session = self._seed(
            MemorySession(),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        session.market_data["ACME"] = [(reference - timedelta(hours=1), 100.0)]
        session.seed_forecast(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            scenario_id=_id("scenario:bull"),
            as_of=reference + timedelta(days=1),
            superseded_at=reference + timedelta(days=2),
        )
        seeded = len(session.forecasts)
        self.assertEqual(_backfill_missing_forecasts(session, reference), 3)
        # Exactly the three new forecast rows were appended; the seeded
        # historical row is untouched.
        self.assertEqual(len(session.forecasts) - seeded, 3)


class EnqueueTests(unittest.TestCase):
    def test_enqueue_uses_shared_job_identity_and_coalesces(self):
        session = RecordingSession()

        class SessionContext:
            def __enter__(self):
                return session

            def __exit__(self, *args):
                return False

        with (
            patch("thesis_autonomy.accept_run", return_value=NOW) as accept,
            patch("thesis_autonomy.start_run", return_value=True),
            patch("thesis_autonomy.finalize_run_safely") as finalize,
            patch("thesis_autonomy.get_session", return_value=SessionContext()),
            patch(
                "thesis_autonomy.enqueue_job",
                return_value=SimpleNamespace(
                    inserted=True,
                    job=SimpleNamespace(id=7, correlation_id="corr-x"),
                ),
            ) as enqueue,
        ):
            result = enqueue_thesis_autonomy_job(
                cycle_config(), triggered_by="scheduler"
            )
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["job_id"], "7")
        self.assertTrue(result["inserted"])
        call = enqueue.call_args
        self.assertEqual(call.kwargs["job_type"], JOB_TYPE)
        self.assertEqual(call.kwargs["dedupe_key"], "thesis-autonomy:global")
        self.assertEqual(
            call.kwargs["payload"],
            {"force": False, "as_of": NOW.isoformat()},
        )
        expected = canonical_fingerprint(
            {
                "job_type": JOB_TYPE,
                "config": thesis_autonomy_identity(cycle_config()),
                "request_date": NOW.date().isoformat(),
                "request_nonce": None,
            }
        )
        self.assertEqual(call.kwargs["input_fingerprint"], expected)
        accept.assert_called_once()
        self.assertEqual(
            accept.call_args.args[2:],
            ("scheduler", "research", "thesis_autonomy"),
        )
        finalize.assert_called_once()
        self.assertEqual(finalize.call_args.kwargs["run_kind"], "research")
        self.assertEqual(finalize.call_args.kwargs["component"], "thesis_autonomy")

    def test_forced_runs_receive_a_unique_identity(self):
        session = RecordingSession()

        class SessionContext:
            def __enter__(self):
                return session

            def __exit__(self, *args):
                return False

        fingerprints = []

        def capture_enqueue(_session, **kwargs):
            fingerprints.append(kwargs["input_fingerprint"])
            return SimpleNamespace(
                inserted=True,
                job=SimpleNamespace(id=1, correlation_id="c"),
            )

        with (
            patch("thesis_autonomy.accept_run", return_value=NOW),
            patch("thesis_autonomy.start_run", return_value=True),
            patch("thesis_autonomy.finalize_run_safely"),
            patch("thesis_autonomy.get_session", return_value=SessionContext()),
            patch("thesis_autonomy.enqueue_job", side_effect=capture_enqueue),
        ):
            enqueue_thesis_autonomy_job(cycle_config(), triggered_by="api", force=False)
            enqueue_thesis_autonomy_job(cycle_config(), triggered_by="api", force=True)
        self.assertEqual(len(fingerprints), 2)
        self.assertNotEqual(fingerprints[0], fingerprints[1])


class HandlerDispatchTests(unittest.TestCase):
    def test_route_job_dispatches_thesis_autonomy_run(self):
        from analysis_job_handlers import route_job

        job = SimpleNamespace(
            job_type=JOB_TYPE,
            correlation_id="corr-1",
            payload={"as_of": NOW.isoformat()},
        )
        with (
            patch("analysis_job_handlers._config", return_value=cycle_config()),
            patch(
                "thesis_autonomy.run_autonomous_thesis_cycle",
                return_value={
                    "status": "completed",
                    "error_count": 0,
                    "cost_usd": 0.25,
                    "promoted_count": 2,
                    "falsification_runs": 1,
                    "source_gate_rejections": 3,
                    "opposition_gate_rejections": 4,
                    "semantic_audit_rejections": 5,
                },
            ) as cycle,
        ):
            result = route_job(RecordingSession(), job)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["cost_usd"], 0.25)
        self.assertEqual(result["promoted_count"], 2)
        self.assertEqual(result["source_gate_rejections"], 3)
        self.assertEqual(result["opposition_gate_rejections"], 4)
        self.assertEqual(result["semantic_audit_rejections"], 5)
        cycle.assert_called_once()
        self.assertEqual(cycle.call_args.kwargs["correlation_id"], "corr-1")
        self.assertEqual(cycle.call_args.kwargs["as_of"], NOW.isoformat())


if __name__ == "__main__":
    unittest.main()
