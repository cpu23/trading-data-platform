"""Shared support fixtures, in-memory sessions, and mock runners for thesis autonomy tests."""

import copy
import json
import os
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
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

from sqlalchemy.dialects import postgresql  # noqa: E402

from research_intelligence.contracts import (  # noqa: E402
    NormalizedEntity,
    NormalizedEvidence,
)
from research_intelligence.contracts import (
    canonical_fingerprint as canonical_fingerprint,
)
from research_intelligence.evidence import (  # noqa: E402
    EvidenceCollection,
    EvidenceRegistry,
)
from thesis_autonomy import (  # noqa: E402
    JOB_TYPE as JOB_TYPE,
)
from thesis_autonomy import (
    LLMChallenger as LLMChallenger,
)
from thesis_autonomy import (
    LLMRoleRunner as LLMRoleRunner,
)
from thesis_autonomy import (
    LLMSemanticCitationAuditor as LLMSemanticCitationAuditor,
)
from thesis_autonomy import (
    _backfill_missing_forecasts as _backfill_missing_forecasts,
)
from thesis_autonomy import (
    _canonical_market_symbol,
    run_autonomous_thesis_cycle,
)
from thesis_autonomy import (
    _count_unversioned_second_pass_candidates as _count_unversioned_second_pass_candidates,
)
from thesis_autonomy import (
    _cycle_key as _cycle_key,
)
from thesis_autonomy import (
    _load_second_pass_snapshot as _load_second_pass_snapshot,
)
from thesis_autonomy import (
    _persist_candidate_risks as _persist_candidate_risks,
)
from thesis_autonomy import (
    _resolve_matured_forecasts as _resolve_matured_forecasts,
)
from thesis_autonomy import (
    _second_pass_candidates as _second_pass_candidates,
)
from thesis_autonomy import (
    _signal as _signal,
)
from thesis_autonomy import (
    enqueue_thesis_autonomy_job as enqueue_thesis_autonomy_job,
)
from thesis_autonomy import (
    thesis_autonomy_identity as thesis_autonomy_identity,
)
from thesis_challenges import ChallengeProposal  # noqa: E402
from thesis_tournament import (  # noqa: E402
    CITATION_FIELDS as CITATION_FIELDS,
)
from thesis_tournament import (
    role_output_schema as role_output_schema,
)

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


