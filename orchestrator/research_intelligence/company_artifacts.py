"""Immutable, self-verifying artifact directories for company-benchmark runs.

``write_immutable_company_run`` publishes one *complete evaluated* single-company
benchmark run as a directory that either does not exist or is complete: the
destination is created exclusively (creation fails if it already exists), every
payload file is written with exclusive-create ``x`` semantics, canonical JSON
bytes, and fsync, and ``manifest.json`` lands last as the completion marker
carrying SHA-256 digests and byte lengths for every preceding file. Any failure
removes the freshly created directory.

A complete run records both halves of the benchmark after judging has finished:
the producer half (case, dispatch request, ordered recorded attempt chain
including the repair prompt that produced each retry, and the finalized output)
and the evaluator half (evaluator fixture packet, hard-gate report, three blind
judge requests, each judge's exact raw response plus its parsed verdict and
per-judge execution/session provenance, the aggregate panel report, and the
defect log). Evaluator material is stored only here, after production output
exists; it never flows back into any request/executor input, and producer-side
payloads are recursively scanned so an evaluator-only key can never leak into
them.

The writer trusts no caller-supplied evaluation object. It derives the
canonical producer identity from the normalized producer fields at the gate
boundary (never from stored ``ProducerCase.fingerprint``), requires the
persisted blind salt plus exactly three raw recorded judge outputs, rebuilds
the judge requests from ``(producer, evaluator, finalized, salt)``, matches
each raw output to its request by role, reparses it through the strict
production parser, recomputes hard gates and the aggregate panel itself, and
enforces one namespace for all judge and producer execution/session
identities. Every mismatch rejects before the
destination directory is created.

``is_complete_company_run`` re-verifies that contract fail-closed: exact
expected file set, no symlinks, unchanged bytes, an exact manifest schema
version, and one run-identity digest binding every manifest field together.
It reloads the producer and evaluator cases from their canonical artifact
envelopes, recomputes the dispatch request, validates the complete ordered
attempt chain before replaying its accepted output through finalization,
recomputes hard gates, and rebuilds all three blind judge requests from the
salt disclosed in ``blind_salt.json`` (persisted only after judges complete,
never embedded in any producer or judge prompt),
reparses every verbatim judge response, re-aggregates the panel and defect
log, and canonical-compares each recomputation against the stored bytes. No
git, subprocess, network, or database access happens here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import investment_service
from research_intelligence.contracts import canonical_fingerprint
from research_intelligence.company_benchmarks import (
    _EVALUATOR_ONLY_KEYS,
    EvaluatorCase,
    ProducerCase,
    canonical_producer_fingerprint,
    load_evaluator_case,
    load_producer_case,
    prepare_company_run,
    recorded_executor_output,
    finalize_recorded_company_run,
)
from research_intelligence.company_judging import (
    PROMPT_VERSION,
    SCHEMA_NAME,
    aggregate_judge_panel,
    build_blind_judge_requests,
    parse_judge_result,
    BlindJudgeRequest,
    JudgeResult,
    SCHEMA_VERSION as JUDGE_SCHEMA_VERSION,
)
from research_intelligence.company_quality import (
    HardGateReport,
    run_company_hard_gates,
)

ARTIFACT_SCHEMA_VERSION = "company_run_artifact_v7"
ARTIFACT_KIND = "complete_company_benchmark_run"

MANIFEST_NAME = "manifest.json"
_PRODUCER_FILE = "producer.json"
_REQUEST_FILE = "request.json"
_ATTEMPTS_FILE = "attempts.json"
_FINALIZED_FILE = "finalized_output.json"
_EVALUATOR_FILE = "evaluator.json"
_HARD_GATES_FILE = "hard_gates.json"
_JUDGE_REQUESTS_FILE = "judge_requests.json"
_JUDGE_RESULTS_FILE = "judge_results.json"
_PANEL_REPORT_FILE = "panel_report.json"
_DEFECT_LOG_FILE = "defect_log.json"
_STAGE_CONFIG_FILE = "stage_config.json"
_BLIND_SALT_FILE = "blind_salt.json"

_PROMPT_STAGE_NAME = "company_blind_judge"

_PAYLOAD_FILES = frozenset(
    {
        _PRODUCER_FILE,
        _REQUEST_FILE,
        _ATTEMPTS_FILE,
        _FINALIZED_FILE,
        _EVALUATOR_FILE,
        _HARD_GATES_FILE,
        _JUDGE_REQUESTS_FILE,
        _JUDGE_RESULTS_FILE,
        _PANEL_REPORT_FILE,
        _DEFECT_LOG_FILE,
        _STAGE_CONFIG_FILE,
        _BLIND_SALT_FILE,
    }
)
_RUN_FILES = _PAYLOAD_FILES | {MANIFEST_NAME}

_FINGERPRINT_RE = re.compile(r"[a-f0-9]{64}")
_GIT_COMMIT_RE = re.compile(r"[a-f0-9]{7,64}")
_MAX_PROVENANCE_KEYS = 32
_MAX_PROVENANCE_KEY_CHARS = 80
_MAX_PROVENANCE_VALUE_CHARS = 300
_MAX_SCAN_DEPTH = 16
_MAX_STAGE_DEPTH = 8
_MAX_STAGE_NODES = 4_096
_MAX_STAGE_KEY_CHARS = 120
_MAX_STAGE_STRING_CHARS = 20_000
_MAX_ATTEMPTS = 8
_MAX_CONTENT_CHARS = 1_000_000
_MAX_REPAIR_PROMPT_CHARS = 2_000
_MAX_EXECUTION_ID_CHARS = 200
_MAX_EXECUTOR_IDENTITY_CHARS = 120
_MAX_RAW_JUDGE_CHARS = 1_000_000
_MAX_SALT_BYTES = 4_096

__all__ = [
    "ARTIFACT_KIND",
    "ARTIFACT_SCHEMA_VERSION",
    "MANIFEST_NAME",
    "RecordedAttempt",
    "is_complete_company_run",
    "write_immutable_company_run",
]



@dataclass(frozen=True, slots=True)
class RecordedAttempt:
    """One recorded executor dispatch in the ordered run chain.

    ``accepted`` marks the attempt whose content was carried into validation
    and finalization; exactly one attempt per run is accepted and it must be
    the last one. A rejected attempt must carry the nonblank repair prompt
    that produced its successor.
    """

    index: int
    content: str
    accepted: bool
    repair_prompt: str | None
    provenance: Mapping[str, Any]


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Encode one artifact payload as canonical JSON bytes.

    Frozen containers (mappings, sequences) are normalized into JSON-native
    structures first, so any immutable input still encodes deterministically.
    """
    return json.dumps(
        _plain(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_matches(left: Any, right: Any, label: str) -> None:
    """Canonical-compare two structures; cross-mixing fails closed."""
    left_blob = _canonical_json(_plain({"value": left}))
    right_blob = _canonical_json(_plain({"value": right}))
    if left_blob != right_blob:
        raise ValueError(f"company run {label} does not match its recomputation")


def _git_commit(value: object) -> str:
    cleaned = value.strip().casefold() if isinstance(value, str) else ""
    if not _GIT_COMMIT_RE.fullmatch(cleaned):
        raise ValueError("company run git_commit must be 7..64 hex chars")
    return cleaned


def _required_fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or not _FINGERPRINT_RE.fullmatch(value):
        raise ValueError(f"company run {label} fingerprint must be SHA-256 hex")
    return value


def _created_at(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("company run created_at must be timezone-aware")
    return value.astimezone(UTC)


def _checked_executor_provenance(provenance: object) -> dict[str, Any]:
    if not isinstance(provenance, Mapping) or len(provenance) > _MAX_PROVENANCE_KEYS:
        raise ValueError(
            "company run executor provenance must be an object of at most "
            f"{_MAX_PROVENANCE_KEYS} keys"
        )
    checked: dict[str, Any] = {}
    for key, value in provenance.items():
        if not isinstance(key, str) or not key or len(key) > _MAX_PROVENANCE_KEY_CHARS:
            raise ValueError("company run executor provenance keys must be short strings")
        if value is not None and not isinstance(value, (str, bool, int, float)):
            raise ValueError("company run executor provenance values must be scalars")
        if isinstance(value, str) and len(value) > _MAX_PROVENANCE_VALUE_CHARS:
            raise ValueError("company run executor provenance strings must be bounded")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("company run executor provenance floats must be finite")
        checked[key] = value
    return checked


def _nonblank_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"company run {label} must be nonblank text")
    if len(value) > maximum:
        raise ValueError(
            f"company run {label} must be at most {maximum} characters"
        )
    return value


def _optional_repair_prompt(value: object) -> str | None:
    if value is None:
        return None
    return _nonblank_text(value, "attempt repair_prompt", _MAX_REPAIR_PROMPT_CHARS)


def _checked_attempts(
    attempts: object, execution_id: str
) -> tuple[RecordedAttempt, ...]:
    """Validate the ordered recorded attempt chain including repair linkage.

    Every attempt must carry this run's stable ``execution_id`` in its
    recorded provenance, so an attempt chain (or a whole interlocking
    component set) transplanted from another execution rejects even when its
    own parts are self-consistent.
    """
    chain = list(attempts)
    if not 1 <= len(chain) <= _MAX_ATTEMPTS:
        raise ValueError(
            f"company run attempts must contain between 1 and {_MAX_ATTEMPTS} entries"
        )
    checked: list[RecordedAttempt] = []
    for position, attempt in enumerate(chain):
        if not isinstance(attempt, RecordedAttempt):
            raise ValueError(
                "company run attempts must be RecordedAttempt instances "
                f"(position {position})"
            )
        if isinstance(attempt.index, bool) or attempt.index != position:
            raise ValueError(
                "company run attempts must be contiguous from zero: attempt "
                f"{position} carries index {attempt.index!r}"
            )
        content = _nonblank_text(
            attempt.content, f"attempt {position} content", _MAX_CONTENT_CHARS
        )
        if not isinstance(attempt.accepted, bool):
            raise ValueError(f"company run attempt {position} accepted must be boolean")
        provenance = _checked_executor_provenance(attempt.provenance)
        recorded_execution = provenance.get("execution_id")
        if recorded_execution != execution_id:
            raise ValueError(
                f"company run attempt {position} provenance execution_id "
                f"{recorded_execution!r} does not match this run's executor "
                f"execution_id {execution_id!r}"
            )
        repair_prompt = _optional_repair_prompt(attempt.repair_prompt)
        checked.append(
            RecordedAttempt(
                index=position,
                content=content,
                accepted=attempt.accepted,
                repair_prompt=repair_prompt,
                provenance=provenance,
            )
        )
    accepted = [attempt for attempt in checked if attempt.accepted]
    if len(accepted) != 1:
        raise ValueError(
            "company run attempts must record exactly one accepted attempt, got "
            f"{len(accepted)}"
        )
    final_attempt = checked[-1]
    if not final_attempt.accepted:
        raise ValueError("the accepted company run attempt must be the last attempt")
    for attempt in checked[:-1]:
        if attempt.repair_prompt is None:
            raise ValueError(
                f"rejected company run attempt {attempt.index} must record the "
                "repair prompt that produced its successor"
            )
    if final_attempt.repair_prompt is not None:
        raise ValueError(
            "the accepted company run attempt must not carry a repair prompt"
        )
    return tuple(checked)

def _producer_identity_namespace(
    attempts: Sequence[RecordedAttempt], execution_id: str
) -> set[str]:
    """Return producer execution/session identities reserved from blind judges."""
    session_ids: set[str] = set()
    for attempt in attempts:
        if "session_id" not in attempt.provenance:
            continue
        raw_session_id = attempt.provenance["session_id"]
        if raw_session_id is None:
            continue
        session_ids.add(
            _nonblank_text(
                raw_session_id,
                f"attempt {attempt.index} provenance session_id",
                _MAX_EXECUTION_ID_CHARS,
            )
        )
    if execution_id in session_ids:
        raise ValueError(
            "company run producer execution_id and session_id must occupy "
            "distinct identity namespaces"
        )
    return {execution_id, *session_ids}


def _checked_stage_config(
    value: object,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate stage configuration and extract exact executor identity.

    The stage config must be a nonempty bounded JSON-safe object carrying a
    top-level ``executor`` object with exactly ``executor_kind``,
    ``execution_id``, ``executor_name``, and ``executor_version``. Returns the
    plain-copy config plus the extracted identity mapping.
    """
    if not isinstance(value, Mapping) or not value:
        raise ValueError(
            "company run stage_config must be a nonempty JSON object"
        )
    budget = {"nodes": 0}

    def visit(node: Any, path: str, depth: int) -> None:
        budget["nodes"] += 1
        if budget["nodes"] > _MAX_STAGE_NODES:
            raise ValueError("company run stage_config exceeds the node budget")
        if depth > _MAX_STAGE_DEPTH:
            raise ValueError(f"company run stage_config nests too deeply at {path}")
        if node is None or isinstance(node, (bool, int)):
            return
        if isinstance(node, float):
            if not math.isfinite(node):
                raise ValueError(
                    f"company run stage_config value at {path} is not JSON-safe"
                )
            return
        if isinstance(node, str):
            if len(node) > _MAX_STAGE_STRING_CHARS:
                raise ValueError(f"company run stage_config string at {path} is too long")
            return
        if isinstance(node, Mapping):
            for key, item in node.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > _MAX_STAGE_KEY_CHARS
                ):
                    raise ValueError(
                        f"company run stage_config key under {path} is invalid"
                    )
                visit(item, f"{path}.{key}", depth + 1)
            return
        raise ValueError(f"company run stage_config value at {path} is not JSON-safe")

    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > _MAX_STAGE_KEY_CHARS:
            raise ValueError("company run stage_config keys must be short nonempty strings")
        visit(item, key, 1)

    raw_identity = value.get("executor")
    if (
        not isinstance(raw_identity, Mapping)
        or set(raw_identity)
        != {
            "executor_kind",
            "execution_id",
            "executor_name",
            "executor_version",
        }
    ):
        raise ValueError(
            "company run stage_config.executor must carry exactly executor_kind, "
            "execution_id, executor_name, and executor_version"
        )
    kind = raw_identity["executor_kind"]
    if kind != "agent_environment":
        raise ValueError('company run executor_kind must be "agent_environment"')
    identity = {
        "executor_kind": kind,
        "execution_id": _nonblank_text(
            raw_identity["execution_id"],
            "stage_config.executor execution_id",
            _MAX_EXECUTION_ID_CHARS,
        ),
        "executor_name": _nonblank_text(
            raw_identity["executor_name"],
            "stage_config.executor executor_name",
            _MAX_EXECUTOR_IDENTITY_CHARS,
        ),
        "executor_version": _nonblank_text(
            raw_identity["executor_version"],
            "stage_config.executor executor_version",
            _MAX_EXECUTOR_IDENTITY_CHARS,
        ),
    }
    return dict(value), identity


def _identity_as_of(as_of: datetime) -> str:
    """Canonical artifact identity text for ``as_of`` (UTC, ``Z``-suffixed)."""
    return as_of.astimezone(UTC).isoformat().replace("+00:00", "Z")


_EVALUATOR_PAYLOAD_KEYS = (
    "schema_version",
    "case_id",
    "fixture_version",
    "producer_fingerprint",
    "expected_material_observations",
    "deterministic_checks",
    "strongest_counter_thesis",
    "expected_unknowns",
    "known_traps",
    "later_outcomes",
    "required_material_evidence",
    "forbidden_hindsight",
)


def _plain_forbidden_claim(claim: object) -> dict[str, Any]:
    """Structured JSON row for one forbidden-hindsight claim."""
    if not isinstance(claim, ForbiddenCompanyClaim):
        raise ValueError(
            "company run forbidden_hindsight must contain ForbiddenCompanyClaim rows"
        )
    return {
        "claim_id": claim.claim_id,
        "metric_aliases": list(claim.metric_aliases),
        "value": claim.value,
        "period_aliases": list(claim.period_aliases),
        "available_after": _identity_as_of(claim.available_after),
    }


def _evaluator_payload(evaluator: EvaluatorCase) -> dict[str, Any]:
    """Canonical evaluator packet bound to the producer fingerprint.

    Stores the exact validated evaluator fixture — including the hindsight
    anchors judges never saw — so the hard-gate report is reproducible from
    artifact bytes alone. ``forbidden_hindsight`` rows serialize as their
    structured fields (never ``repr`` text), keeping the envelope reloadable
    through the exact evaluator loader keys. Location-dependent
    ``source_path`` is omitted: the manifest owns derived identities.
    """
    payload: dict[str, Any] = {}
    for key in _EVALUATOR_PAYLOAD_KEYS:
        value = getattr(evaluator, key)
        if key == "forbidden_hindsight":
            payload[key] = [_plain_forbidden_claim(item) for item in value]
        elif isinstance(value, (list, tuple)):
            payload[key] = [_plain(item) for item in value]
        elif isinstance(value, Mapping):
            payload[key] = _plain(dict(value))
        else:
            payload[key] = _plain(value)
    return payload


def _reject_evaluator_keys(node: Any, path: str, *, depth: int = 0) -> None:
    """Refuse any evaluator-half key leaking into a recorded producer payload."""

    if depth > _MAX_SCAN_DEPTH:
        raise ValueError("company run producer payload nests too deeply")
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(key, str) and key in _EVALUATOR_ONLY_KEYS:
                raise ValueError(
                    f"evaluator-only key leaked into producer payload: {path}.{key}"
                )
            _reject_evaluator_keys(value, f"{path}.{key}", depth=depth + 1)
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            _reject_evaluator_keys(item, f"{path}[{index}]", depth=depth + 1)


def _producer_payload(producer: ProducerCase, producer_fingerprint: str) -> dict[str, Any]:
    """Canonical producer.json envelope the exact loader keys can replay.

    Serializes exactly the defined producer identity fields plus the DERIVED
    ``fingerprint`` (an asserted copy of the canonical identity, validated
    on reload). Location-dependent ``source_path`` is deliberately omitted:
    the manifest owns derived identities and the envelope must reload
    byte-identically on any machine.
    """
    # An EvaluatorCase is never a ProducerCase, so the isinstance gate below
    # already refuses an evaluator packet passed in the producer slot.
    if not isinstance(producer, ProducerCase):
        raise ValueError("company run producer must be a ProducerCase")
    payload: dict[str, Any] = {
        "schema_version": producer.schema_version,
        "case_id": producer.case_id,
        "fixture_version": producer.fixture_version,
        "as_of": _identity_as_of(producer.as_of),
        "document": dict(producer.document),
        "excerpt": producer.excerpt,
        "deterministic_current": dict(producer.deterministic_current),
        "deterministic_prior": dict(producer.deterministic_prior),
        "market_inputs": dict(producer.market_inputs),
        "prior_facts": dict(producer.prior_facts),
        "previous_state": producer.previous_state,
        "prior_count": producer.prior_count,
        "news_items": [dict(item) for item in producer.news_items],
        "extraction": dict(producer.extraction),
        "fingerprint": producer_fingerprint,
    }
    _reject_evaluator_keys(payload, "producer")
    return payload


def _request_payload(request: object) -> dict[str, Any]:
    if not isinstance(request, investment_service.InvestmentAnalysisRequest):
        raise ValueError("company run request must be an InvestmentAnalysisRequest")
    return {
        "prompt": request.prompt,
        "schema_name": request.schema_name,
        "strict": request.strict,
        "schema": request.schema,
        "relationship_facts": request.relationship_facts,
        "material_relationships": request.material_relationships,
        "fingerprint": request.fingerprint,
    }


def _attempts_payload(attempts: Sequence[RecordedAttempt]) -> dict[str, Any]:
    return {
        "attempts": [
            {
                "index": attempt.index,
                "content": attempt.content,
                "accepted": attempt.accepted,
                "repair_prompt": attempt.repair_prompt,
                "provenance": dict(attempt.provenance),
            }
            for attempt in attempts
        ]
    }


def _finalized_payload(
    finalized: "investment_service.InvestmentFinalizedAnalysis",
    producer: ProducerCase,
) -> dict[str, Any]:
    if not isinstance(finalized, investment_service.InvestmentFinalizedAnalysis):
        raise ValueError("company run finalized analysis must be an InvestmentFinalizedAnalysis")
    # previous_state/prior_count are finalization inputs owned by the producer
    # case; they are recorded beside the finalized outputs for replay context.
    payload = {
        "facts": finalized.facts,
        "analysis": finalized.analysis,
        "previous_facts": finalized.previous_facts,
        "previous_state": producer.previous_state,
        "prior_count": producer.prior_count,
    }
    _reject_evaluator_keys(payload, "finalized_output")
    return payload


def _producer_envelope(raw: Any) -> ProducerCase:
    """Reload a :class:`ProducerCase` from its canonical producer.json bytes.

    Accepts exactly the loader's producer key set plus the asserted derived
    ``fingerprint``. The production parser revalidates every field, then the
    asserted fingerprint is compared (constant-time) against the freshly
    derived canonical identity, so a mutated envelope cannot carry a rebound
    identity past this boundary.
    """
    if not isinstance(raw, Mapping) or "fingerprint" not in raw:
        raise ValueError(
            "company run producer envelope has unexpected or missing fields"
        )
    return load_producer_case(raw)


def _evaluator_envelope(raw: Any, producer: ProducerCase) -> EvaluatorCase:
    """Reload an :class:`EvaluatorCase` from its canonical evaluator.json bytes."""
    if not isinstance(raw, Mapping):
        raise ValueError(
            "company run evaluator envelope has unexpected or missing fields"
        )
    return load_evaluator_case(raw, producer=producer)





def _plain(value: Any) -> Any:
    """Normalize frozen/mapping containers into JSON-native structures."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, bool)) or value is None:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value
    return str(value)





def _plain_judge_request(request: BlindJudgeRequest) -> dict[str, Any]:
    return {
        "role": request.role,
        "token": request.token,
        "prompt_version": request.prompt_version,
        "schema_name": request.schema_name,
        "strict": request.strict,
        "schema": _plain(request.schema),
        "prompt": request.prompt,
        "producer_fingerprint": request.producer_fingerprint,
        "fingerprint": request.fingerprint,
        "response_binding": request.response_binding,
    }




def _panel_report_payload(panel_report: object) -> dict[str, Any]:
    to_dict = getattr(panel_report, "to_dict", None)
    if to_dict is None or not callable(to_dict):
        raise ValueError("company run panel report must expose to_dict()")
    payload = to_dict()
    if not isinstance(payload, Mapping):
        raise ValueError("company run panel report must encode to a JSON object")
    return dict(payload)


def _defect_log_payload(
    results: Sequence[JudgeResult],
    panel_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Deterministic defect log over judge defects and gate failures.

    Judge-sourced defects keep their role binding and sort by
    ``(role, defect)``; gate failures reuse the stable hard-gate ordering.
    """
    defects: list[dict[str, str]] = []
    for result in results:
        for defect in result.concrete_defects:
            defects.append({"source": result.role, "defect": defect})
    defects.sort(key=lambda entry: (entry["source"], entry["defect"]))
    failures = panel_report.get("gate_failures")
    gate_failures = [failure for failure in failures] if isinstance(failures, list) else []
    return {
        "judge_defects": defects,
        "gate_failures": gate_failures,
    }


def _exclusive_write(path: Path, blob: bytes) -> None:
    """Create ``path`` exclusively (``x`` mode), write, and fsync it."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _discard_directory(root: Path, written: list[Path]) -> None:
    """Best-effort removal of a run directory this call created incompletely."""
    for path in written:
        try:
            path.unlink()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _checked_evaluator_identity(evaluator: object, producer: ProducerCase, producer_fingerprint: str) -> None:
    """Anchor the supplied evaluator half to this exact producer case.

    The pairing fields are checked against the DERIVED canonical producer
    identity (never the stored ``ProducerCase.fingerprint``), so a case
    whose stored fingerprint was rebound cannot drag a foreign evaluator
    packet through.
    """
    if not isinstance(evaluator, EvaluatorCase):
        raise ValueError("company run evaluator must be an EvaluatorCase")
    if (
        evaluator.case_id != producer.case_id
        or evaluator.fixture_version != producer.fixture_version
        or evaluator.producer_fingerprint != producer_fingerprint
    ):
        raise ValueError(
            "company run evaluator does not match this run's producer case"
        )


def _checked_salt(blind_salt: object) -> bytes:
    """Require a nonempty bounded blind salt; return its exact bytes."""
    if isinstance(blind_salt, str):
        encoded = blind_salt.encode("utf-8")
    elif isinstance(blind_salt, (bytes, bytearray)):
        encoded = bytes(blind_salt)
    else:
        raise ValueError("company run blind_salt must be str or bytes")
    if not 0 < len(encoded) <= _MAX_SALT_BYTES:
        raise ValueError(
            f"company run blind_salt must be 1..{_MAX_SALT_BYTES} bytes"
        )
    return encoded


def _salt_commitment(salt: bytes) -> str:
    """Commitment persisted in the manifest for the exact salt bytes."""
    return hashlib.sha256(
        b"research_intelligence/company_run/blind_salt\x00" + salt
    ).hexdigest()


def _hex_salt(salt: bytes) -> str:
    """Lowercase hex disclosure of the exact salt bytes."""
    return salt.hex()


def _salt_from_hex(value: object) -> bytes:
    """Exact salt bytes from the disclosed ``salt_hex`` field."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{2})+", value):
        raise ValueError(
            "company run blind_salt.json salt_hex must be even-length lowercase hex"
        )
    encoded = bytes.fromhex(value)
    if len(encoded) > _MAX_SALT_BYTES:
        raise ValueError(
            f"company run blind_salt.json must disclose 1..{_MAX_SALT_BYTES} bytes"
        )
    return encoded


def _blind_salt_payload(salt: bytes) -> dict[str, str]:
    """Digested payload disclosing the salt only after judging completes."""
    return {"salt_hex": _hex_salt(salt)}


def _run_identity_digest(fields: Mapping[str, Any]) -> str:
    """Digest binding every other manifest identity field together.

    Domain-separated over the canonical encoding of the complete manifest
    field map (including the exact per-file digest specs), so deleting or
    altering any isolated manifest field — the blind-salt commitment
    included — breaks this digest too.
    """
    return hashlib.sha256(
        b"research_intelligence/company_run/run_identity_v1\x00"
        + _canonical_json({"fields": dict(fields)})
    ).hexdigest()


_JUDGE_RECORD_KEYS = frozenset(
    {"role", "token", "raw_json", "execution_id", "session_id", "provenance"}
)


def _checked_judge_records(
    judge_records: object, reserved_identities: set[str]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate the three raw recorded judge outputs and their provenance.

    Each record must be a plain mapping carrying exactly ``role``,
    ``token``, verbatim ``raw_json`` output text, distinct nonblank
    ``execution_id`` and ``session_id`` identities, and bounded flat scalar
    ``provenance``. All judge execution/session identities share one global
    namespace with the producer execution/session identities supplied in
    ``reserved_identities``. Returns records keyed by role plus the ordered
    role list.
    """
    if isinstance(judge_records, (str, bytes)) or not isinstance(judge_records, Sequence):
        raise ValueError("company run judge_records must be a sequence")
    records = list(judge_records)
    if len(records) != 3:
        raise ValueError("company run requires exactly three judge_records entries")
    by_role: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    identity_namespace = set(reserved_identities)
    for position, record in enumerate(records):
        if not isinstance(record, Mapping) or set(record) != _JUDGE_RECORD_KEYS:
            raise ValueError(
                f"judge_records[{position}] must carry exactly role, token, "
                "raw_json, execution_id, session_id, provenance"
            )
        role = _nonblank_text(record["role"], f"judge_records[{position}].role", 80)
        token = _nonblank_text(record["token"], f"judge_records[{position}].token", 64)
        raw_json = record["raw_json"]
        if (
            not isinstance(raw_json, str)
            or not raw_json.strip()
            or len(raw_json) > _MAX_RAW_JUDGE_CHARS
        ):
            raise ValueError(
                f"judge_records[{position}].raw_json must be nonblank bounded "
                "verbatim response text"
            )
        execution_id = _nonblank_text(
            record["execution_id"],
            f"judge_records[{position}].execution_id",
            _MAX_EXECUTION_ID_CHARS,
        )
        session_id = _nonblank_text(
            record["session_id"],
            f"judge_records[{position}].session_id",
            _MAX_EXECUTION_ID_CHARS,
        )
        provenance = _checked_executor_provenance(record["provenance"])
        if role in by_role:
            raise ValueError(f"duplicate judge_records entry for role {role}")
        for identity_kind, identity in (
            ("execution_id", execution_id),
            ("session_id", session_id),
        ):
            if identity in identity_namespace:
                raise ValueError(
                    f"judge {identity_kind} {identity!r} reuses a producer or "
                    "blind-judge identity; all execution and session identities "
                    "must occupy one global namespace"
                )
            identity_namespace.add(identity)
        by_role[role] = {
            "token": token,
            "raw_json": raw_json,
            "execution_id": execution_id,
            "session_id": session_id,
            "provenance": provenance,
        }
        order.append(role)
    return by_role, order


def _judge_provenance_block(
    record: Mapping[str, Any], request: BlindJudgeRequest
) -> dict[str, Any]:
    """Per-judge provenance block persisted beside each parsed verdict."""
    return {
        "execution_id": record["execution_id"],
        "session_id": record["session_id"],
        "judge_provenance": _plain(dict(record["provenance"])),
        "request_fingerprint": request.fingerprint,
        "response_binding": request.response_binding,
    }
def write_immutable_company_run(
    output_dir: str | Path,
    *,
    producer: ProducerCase,
    request: "investment_service.InvestmentAnalysisRequest",
    attempts: Sequence[RecordedAttempt],
    finalized: "investment_service.InvestmentFinalizedAnalysis",
    evaluator: object,
    blind_salt: str | bytes,
    judge_records: Sequence[Mapping[str, Any]],
    git_commit: str,
    git_dirty: bool,
    created_at: datetime,
    stage_config: Mapping[str, Any],
) -> Path:
    """Publish one completed, fully judged company-benchmark run immutably.

    Every payload and the manifest are validated and encoded before the
    destination is created. The writer trusts no caller-supplied evaluation
    object: it derives the canonical producer identity from the normalized
    producer fields, recomputes the dispatch request, replays the final
    accepted attempt through production validation/finalization, recomputes
    hard gates, rebuilds all three blind judge requests from
    ``(producer, evaluator, finalized, blind_salt)``, reparses each raw
    recorded judge output through the strict parser, and re-aggregates the
    panel. The supplied ``request`` and ``attempts`` must match their
    recomputations exactly; judge records must pair with the rebuilt
    requests by role and token, with every judge and producer
    execution/session identity occupying one global namespace. The stored
    ``ProducerCase.fingerprint`` must equal the derived canonical identity
    (compared constant-time) before any downstream artifact is derived. Any
    mismatch fails closed before a byte exists on disk.
    """
    stage_data, executor_identity = _checked_stage_config(stage_config)
    attempts_chain = _checked_attempts(attempts, executor_identity["execution_id"])
    producer_identities = _producer_identity_namespace(
        attempts_chain, executor_identity["execution_id"]
    )
    attempts_data = _attempts_payload(attempts_chain)
    commit = _git_commit(git_commit)
    if not isinstance(git_dirty, bool):
        raise ValueError("company run git_dirty must be a boolean")
    stamp = _created_at(created_at)

    # Canonical identity is derived at this trust boundary; stored
    # ProducerCase.fingerprint data is never trusted.
    if not isinstance(producer, ProducerCase):
        raise ValueError("company run producer must be a ProducerCase")
    producer_fingerprint = canonical_producer_fingerprint(producer)
    stored_fingerprint = producer.fingerprint
    if (
        not isinstance(stored_fingerprint, str)
        or not hmac.compare_digest(stored_fingerprint, producer_fingerprint)
    ):
        raise ValueError(
            "company run rebound producer case rejected: stored producer "
            "fingerprint does not match the canonical identity derived from "
            "the normalized producer fields"
        )
    producer_data = _producer_payload(producer, producer_fingerprint)

    _checked_evaluator_identity(evaluator, producer, producer_fingerprint)
    salt = _checked_salt(blind_salt)
    by_role, record_order = _checked_judge_records(
        judge_records, producer_identities
    )

    recomputed_request = prepare_company_run(producer)
    request_data = _request_payload(request)
    _canonical_matches(
        _request_payload(recomputed_request), request_data, "producer request"
    )

    last_accepted = attempts_chain[-1]
    replay_recorded = recorded_executor_output(
        last_accepted.content, last_accepted.provenance
    )
    recomputed_finalized = finalize_recorded_company_run(replay_recorded, producer)
    finalized_data = _finalized_payload(finalized, producer)
    _canonical_matches(
        _finalized_payload(recomputed_finalized, producer),
        finalized_data,
        "final output",
    )

    recomputed_gates = run_company_hard_gates(
        producer, evaluator, recomputed_finalized
    )
    gates_data = _hard_gate_report_payload(recomputed_gates)

    # Rebuild the exact dispatch inputs from trusted content plus salt, then
    # match each raw recorded output to its request BY ROLE and reparse it.
    rebuilt_requests = build_blind_judge_requests(
        producer, evaluator, recomputed_finalized, salt
    )
    requests_by_role: dict[str, BlindJudgeRequest] = {}
    encoded_requests: list[dict[str, Any]] = []
    request_fingerprints: list[str] = []
    for rebuilt in rebuilt_requests:
        record = by_role.get(rebuilt.role)
        if record is None:
            raise ValueError(
                f"judge_records is missing an entry for rebuilt role {rebuilt.role}"
            )
        if record["token"] != rebuilt.token:
            raise ValueError(
                f"judge_records token for role {rebuilt.role} does not match "
                "the rebuilt blind judge request"
            )
        try:
            parsed_result = parse_judge_result(rebuilt, record["raw_json"])
        except ValueError as error:
            raise ValueError(
                f"raw recorded judge response for role {rebuilt.role} does not "
                f"reparse against its rebuilt request: {error}"
            ) from error
        requests_by_role[rebuilt.role] = rebuilt
        encoded_requests.append(_plain_judge_request(rebuilt))
        request_fingerprints.append(rebuilt.fingerprint)
        record["parsed"] = parsed_result
    judge_requests_data = {"requests": encoded_requests}

    ordered_results: list[JudgeResult] = []
    encoded_results: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    for role in record_order:
        record = by_role[role]
        result = record["parsed"]
        if result.role in seen_roles:
            raise ValueError(f"duplicate judge result for role {result.role}")
        seen_roles.add(result.role)
        entry = {
            "role": result.role,
            "token": result.token,
            "prompt_version": result.prompt_version,
            "response_binding": result.response_binding,
            "overall": result.overall,
            "dimension_scores": [
                {
                    "dimension": score.dimension,
                    "score": score.score,
                    "rationale": score.rationale,
                }
                for score in result.scores
            ],
            "concrete_defects": list(result.concrete_defects),
            "severe_regression": result.severe_regression,
            "severe_regression_reason": result.severe_regression_reason,
            "abstained": result.abstained,
            "abstention_reason": result.abstention_reason,
            "raw_response": record["raw_json"],
            **_judge_provenance_block(record, requests_by_role[role]),
        }
        encoded_results.append(entry)
        ordered_results.append(result)
    judge_results_data = {"results": encoded_results}

    recomputed_panel = aggregate_judge_panel(
        list(requests_by_role.values()), ordered_results, recomputed_gates
    )
    panel_data = _panel_report_payload(recomputed_panel)
    defect_log_data = _defect_log_payload(ordered_results, panel_data)
    evaluator_data = _evaluator_payload(evaluator)

    documents: list[tuple[str, bytes]] = [
        (_PRODUCER_FILE, _canonical_json(producer_data)),
        (_REQUEST_FILE, _canonical_json(request_data)),
        (_ATTEMPTS_FILE, _canonical_json(attempts_data)),
        (_FINALIZED_FILE, _canonical_json(finalized_data)),
        (_EVALUATOR_FILE, _canonical_json(evaluator_data)),
        (_HARD_GATES_FILE, _canonical_json(gates_data)),
        (_JUDGE_REQUESTS_FILE, _canonical_json(judge_requests_data)),
        (_JUDGE_RESULTS_FILE, _canonical_json(judge_results_data)),
        (_PANEL_REPORT_FILE, _canonical_json(panel_data)),
        (_DEFECT_LOG_FILE, _canonical_json(defect_log_data)),
        (_STAGE_CONFIG_FILE, _canonical_json(stage_data)),
        (_BLIND_SALT_FILE, _canonical_json(_blind_salt_payload(salt))),
    ]
    file_entries = {
        name: {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
        for name, blob in documents
    }
    if set(file_entries) != _PAYLOAD_FILES or len(documents) != len(_PAYLOAD_FILES):
        raise ValueError(
            "company run manifest must digest every payload file exactly once"
        )
    manifest_fields: dict[str, Any] = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_kind": ARTIFACT_KIND,
        "case_id": producer.case_id,
        "fixture_version": producer.fixture_version,
        "producer_fingerprint": producer_fingerprint,
        "request_fingerprint": request.fingerprint,
        "judge_request_fingerprints": request_fingerprints,
        "blind_salt_commitment": _salt_commitment(salt),
        "prompt_stage": {
            "name": _PROMPT_STAGE_NAME,
            "prompt_version": PROMPT_VERSION,
            "schema_version": JUDGE_SCHEMA_VERSION,
            "schema_name": SCHEMA_NAME,
        },
        "git_commit": commit,
        "git_dirty": git_dirty,
        "created_at": stamp.isoformat(),
        "executor": executor_identity,
        "executor_recorded_provenance": dict(last_accepted.provenance),
        "files": file_entries,
    }
    # The run-identity digest covers every other manifest field, so any
    # isolated manifest mutation — the blind-salt commitment included — is
    # detectable without knowledge of the salt itself.
    manifest_blob = _canonical_json(
        {
            **manifest_fields,
            "run_identity_digest": _run_identity_digest(manifest_fields),
        }
    )

    root = Path(output_dir)
    root.mkdir(mode=0o700)  # Exclusive: FileExistsError when destination exists.
    written: list[Path] = []
    try:
        for name, blob in documents:
            target = root / name
            written.append(target)
            _exclusive_write(target, blob)
        marker = root / MANIFEST_NAME
        written.append(marker)
        _exclusive_write(marker, manifest_blob)
        _fsync_directory(root)
    except BaseException:
        _discard_directory(root, written)
        raise
    return root


def _hard_gate_report_payload(report: object) -> dict[str, Any]:
    if not isinstance(report, HardGateReport):
        raise ValueError("company run hard-gate report must be a HardGateReport")
    return {
        "passed": report.passed,
        "failures": [
            {
                "code": failure.code,
                "severity": failure.severity,
                "root_category": failure.root_category,
                "path": failure.path,
                "observed": failure.observed,
                "expected": failure.expected,
                "evidence": failure.evidence,
            }
            for failure in report.failures
        ],
        "producer_fingerprint": report.producer_fingerprint,
    }


def is_complete_company_run(path: str | Path) -> bool:
    """Return True only for a fully written, unmodified, self-consistent run.

    Beyond the byte-level contract (exact expected file set, no symlinks,
    unchanged digests and lengths), the manifest must carry the exact v7
    identity schema plus a ``run_identity_digest`` binding every other
    manifest field together, so deleting or altering any isolated manifest
    field — the blind-salt commitment included — fails closed even before
    semantic replay begins. The disclosed salt in ``blind_salt.json`` must
    recompute the domain-separated commitment, and every rubric-dependent
    relation is recomputed from the artifact bytes themselves: the producer
    and evaluator envelopes are reloaded through the production loaders, the
    complete attempt chain is revalidated before its accepted output is
    replayed, hard gates are recomputed, and all three blind judge requests
    are rebuilt from ``(producer, evaluator, finalized, salt)``, every verbatim
    response is reparsed through the strict production parser, and the
    panel report and defect log are re-aggregated. Each recomputation is
    canonical-compared against the stored bytes before True is returned.
    No git, subprocess, network, or database access happens here.
    """
    root = Path(path)
    try:
        if root.is_symlink() or not root.is_dir():
            return False
        entries = list(root.iterdir())
        if {entry.name for entry in entries} != _RUN_FILES:
            return False
        for entry in entries:
            if entry.is_symlink() or not entry.is_file():
                return False


        blobs = {name: (root / name).read_bytes() for name in _PAYLOAD_FILES}
        manifest = json.loads((root / MANIFEST_NAME).read_bytes().decode("utf-8"))

        # -- exact manifest schema --------------------------------------
        if not isinstance(manifest, Mapping):
            return False
        required_manifest_keys = {
            "schema_version",
            "artifact_kind",
            "case_id",
            "fixture_version",
            "producer_fingerprint",
            "request_fingerprint",
            "judge_request_fingerprints",
            "blind_salt_commitment",
            "run_identity_digest",
            "prompt_stage",
            "git_commit",
            "git_dirty",
            "created_at",
            "executor",
            "executor_recorded_provenance",
            "files",
        }
        if set(manifest) != required_manifest_keys:
            return False
        if manifest["schema_version"] != ARTIFACT_SCHEMA_VERSION:
            return False
        if manifest["artifact_kind"] != ARTIFACT_KIND:
            return False

        recorded_files = manifest["files"]
        if not isinstance(recorded_files, Mapping) or set(recorded_files) != _PAYLOAD_FILES:
            return False
        for name in sorted(_PAYLOAD_FILES):
            spec = recorded_files[name]
            if not isinstance(spec, Mapping) or set(spec) != {"sha256", "bytes"}:
                return False
            digest = spec["sha256"]
            size = spec["bytes"]
            if (
                not isinstance(digest, str)
                or not _FINGERPRINT_RE.fullmatch(digest)
                or isinstance(size, bool)
                or not isinstance(size, int)
            ):
                return False
            blob = blobs[name]
            if size != len(blob):
                return False
            if not hmac.compare_digest(hashlib.sha256(blob).hexdigest(), digest):
                return False

        # -- run-identity digest binds every other manifest field --------
        manifest_fields = {
            key: manifest[key]
            for key in required_manifest_keys - {"run_identity_digest"}
        }
        if not hmac.compare_digest(
            _run_identity_digest(manifest_fields),
            manifest["run_identity_digest"],
        ):
            return False

        # -- payload shapes ---------------------------------------------
        payloads: dict[str, Any] = {}
        for name in _PAYLOAD_FILES:
            payloads[name] = json.loads(blobs[name].decode("utf-8"))
        producer_payload = payloads[_PRODUCER_FILE]
        request_payload = payloads[_REQUEST_FILE]
        attempts_payload = payloads[_ATTEMPTS_FILE]
        evaluator_payload = payloads[_EVALUATOR_FILE]
        finalized_payload = payloads[_FINALIZED_FILE]
        judge_requests_payload = payloads[_JUDGE_REQUESTS_FILE]
        judge_results_payload = payloads[_JUDGE_RESULTS_FILE]
        panel_payload = payloads[_PANEL_REPORT_FILE]
        defect_log_payload = payloads[_DEFECT_LOG_FILE]
        stage_payload = payloads[_STAGE_CONFIG_FILE]
        hard_gates_payload = payloads[_HARD_GATES_FILE]
        if not all(
            isinstance(payloads[name], Mapping)
            for name in _PAYLOAD_FILES
        ):
            return False

        attempts_entries = attempts_payload.get("attempts")
        judge_request_entries = judge_requests_payload.get("requests")
        judge_result_entries = judge_results_payload.get("results")
        if (
            set(attempts_payload) != {"attempts"}
            or not isinstance(attempts_entries, list)
            or not isinstance(judge_request_entries, list)
            or len(judge_request_entries) != 3
            or not isinstance(judge_result_entries, list)
            or len(judge_result_entries) != 3
        ):
            return False

        attempt_keys = {
            "index",
            "content",
            "accepted",
            "repair_prompt",
            "provenance",
        }
        reconstructed_attempts: list[RecordedAttempt] = []
        for entry in attempts_entries:
            if not isinstance(entry, Mapping) or set(entry) != attempt_keys:
                return False
            reconstructed_attempts.append(
                RecordedAttempt(
                    index=entry["index"],
                    content=entry["content"],
                    accepted=entry["accepted"],
                    repair_prompt=entry["repair_prompt"],
                    provenance=entry["provenance"],
                )
            )
        _, executor_identity = _checked_stage_config(stage_payload)
        attempts_chain = _checked_attempts(
            reconstructed_attempts, executor_identity["execution_id"]
        )
        producer_identities = _producer_identity_namespace(
            attempts_chain, executor_identity["execution_id"]
        )
        last_accepted = attempts_chain[-1]

        # -- manifest identities versus digested payload contents --------
        if manifest["case_id"] != producer_payload.get("case_id"):
            return False
        if manifest["fixture_version"] != producer_payload.get("fixture_version"):
            return False
        if manifest["producer_fingerprint"] != producer_payload.get("fingerprint"):
            return False
        if manifest["request_fingerprint"] != request_payload.get("fingerprint"):
            return False
        if manifest["producer_fingerprint"] != hard_gates_payload.get(
            "producer_fingerprint"
        ):
            return False
        if manifest["producer_fingerprint"] != evaluator_payload.get(
            "producer_fingerprint"
        ):
            return False
        if manifest["producer_fingerprint"] != panel_payload.get(
            "producer_fingerprint"
        ):
            return False
        if manifest["case_id"] != evaluator_payload.get("case_id"):
            return False
        if manifest["fixture_version"] != evaluator_payload.get("fixture_version"):
            return False

        rebuilt_request_fingerprints = []
        seen_roles: set[str] = set()
        results_by_role: dict[str, Mapping[str, Any]] = {}
        for result in judge_result_entries:
            if not isinstance(result, Mapping):
                return False
            role = result.get("role")
            if not isinstance(role, str) or role in seen_roles:
                return False
            seen_roles.add(role)
            results_by_role[role] = result
        _checked_judge_records(
            [
                {
                    "role": result.get("role"),
                    "token": result.get("token"),
                    "raw_json": result.get("raw_response"),
                    "execution_id": result.get("execution_id"),
                    "session_id": result.get("session_id"),
                    "provenance": result.get("judge_provenance"),
                }
                for result in judge_result_entries
            ],
            producer_identities,
        )
        for entry in judge_request_entries:
            if not isinstance(entry, Mapping):
                return False
            fingerprint = entry.get("fingerprint")
            if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint):
                return False
            rebuilt_request_fingerprints.append(fingerprint)
            role = entry.get("role")
            token = entry.get("token")
            binding = entry.get("response_binding")
            if not isinstance(role, str) or not isinstance(token, str) or not isinstance(binding, str):
                return False
            result = results_by_role.get(role)
            if (
                result is None
                or result.get("token") != token
                or result.get("prompt_version") != entry.get("prompt_version")
                or result.get("response_binding") != binding
            ):
                return False
            if not isinstance(result.get("raw_response"), str):
                return False
        if set(results_by_role) != seen_roles or len(seen_roles) != 3:
            return False
        if manifest["judge_request_fingerprints"] != rebuilt_request_fingerprints:
            return False

        prompt_stage = manifest["prompt_stage"]
        if (
            not isinstance(prompt_stage, Mapping)
            or prompt_stage.get("name") != _PROMPT_STAGE_NAME
            or prompt_stage.get("prompt_version") != PROMPT_VERSION
            or prompt_stage.get("schema_version") != JUDGE_SCHEMA_VERSION
            or prompt_stage.get("schema_name") != SCHEMA_NAME
        ):
            return False
        for entry in judge_request_entries:
            if (
                entry.get("prompt_version") != PROMPT_VERSION
                or entry.get("schema_name") != SCHEMA_NAME
            ):
                return False

        # -- disclosed blind salt must recompute the commitment ---------
        # The exact salt is persisted only here, after judging completed;
        # it never appears in any producer or judge prompt. Byte-only
        # verification recomputes the domain-separated commitment from it
        # and then rebuilds every rubric-dependent relation below.
        salt_disclosure = payloads[_BLIND_SALT_FILE]
        if (
            not isinstance(salt_disclosure, Mapping)
            or set(salt_disclosure) != {"salt_hex"}
            or not isinstance(salt_disclosure.get("salt_hex"), str)
        ):
            return False
        try:
            salt = _salt_from_hex(salt_disclosure["salt_hex"])
        except ValueError:
            return False
        if not hmac.compare_digest(_salt_commitment(salt), manifest["blind_salt_commitment"]):
            return False

        git_commit = manifest["git_commit"]
        if (
            not isinstance(git_commit, str)
            or not _GIT_COMMIT_RE.fullmatch(git_commit)
        ):
            return False
        if not isinstance(manifest["git_dirty"], bool):
            return False
        created_at = datetime.fromisoformat(str(manifest["created_at"]))
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            return False

        stage_identity = executor_identity
        executor_manifest = manifest["executor"]
        if (
            not isinstance(executor_manifest, Mapping)
            or dict(executor_manifest) != stage_identity
        ):
            return False
        recorded_provenance = last_accepted.provenance
        if manifest["executor_recorded_provenance"] != dict(recorded_provenance):
            return False

        # -- semantic replay from artifact bytes alone --------------------
        # Reload both cases through the production loaders (exact key sets,
        # full revalidation, asserted producer fingerprint re-checked),
        # replay the accepted attempt through finalization, recompute hard
        # gates, rebuild the three blind judge requests from the verified
        # salt, reparse each verbatim judge response, and canonical-compare
        # every recomputation against the stored bytes.
        producer = _producer_envelope(producer_payload)
        evaluator = _evaluator_envelope(evaluator_payload, producer)
        if manifest["case_id"] != producer.case_id or manifest[
            "fixture_version"
        ] != producer.fixture_version:
            return False
        if hmac.compare_digest(manifest["producer_fingerprint"], producer.fingerprint) is False:
            return False

        recomputed_request = prepare_company_run(producer)
        _canonical_matches(
            _request_payload(recomputed_request), request_payload, "replayed producer request"
        )

        replay_recorded = recorded_executor_output(
            last_accepted.content, last_accepted.provenance
        )
        recomputed_finalized = finalize_recorded_company_run(replay_recorded, producer)
        _canonical_matches(
            _finalized_payload(recomputed_finalized, producer),
            finalized_payload,
            "replayed final output",
        )

        recomputed_gates = run_company_hard_gates(
            producer, evaluator, recomputed_finalized
        )
        _canonical_matches(
            _hard_gate_report_payload(recomputed_gates),
            hard_gates_payload,
            "replayed hard-gate report",
        )
        rebuilt_requests = build_blind_judge_requests(
            producer, evaluator, recomputed_finalized, salt
        )
        parsed_results: list[JudgeResult] = []
        rebuilt_by_role: dict[str, BlindJudgeRequest] = {}
        rebuilt_request_fingerprints: list[str] = []
        for rebuilt in rebuilt_requests:
            entry = next(
                (
                    candidate
                    for candidate in judge_request_entries
                    if isinstance(candidate, Mapping)
                    and candidate.get("role") == rebuilt.role
                ),
                None,
            )
            stored_result = results_by_role.get(rebuilt.role)
            if (
                entry is None
                or stored_result is None
                or entry.get("token") != rebuilt.token
                or stored_result.get("token") != rebuilt.token
            ):
                return False
            raw_response = stored_result.get("raw_response")
            if not isinstance(raw_response, str):
                return False
            try:
                parsed = parse_judge_result(rebuilt, raw_response)
            except ValueError:
                return False
            # The stored parsed verdict must equal its reparse exactly —
            # including the verbatim raw envelope it claims to carry — so
            # editing parsed fields without rewriting the raw response
            # fails closed.
            _canonical_matches(
                {
                    "role": parsed.role,
                    "token": parsed.token,
                    "prompt_version": parsed.prompt_version,
                    "response_binding": parsed.response_binding,
                    "overall": parsed.overall,
                    "dimension_scores": [
                        {
                            "dimension": score.dimension,
                            "score": score.score,
                            "rationale": score.rationale,
                        }
                        for score in parsed.scores
                    ],
                    "concrete_defects": list(parsed.concrete_defects),
                    "severe_regression": parsed.severe_regression,
                    "severe_regression_reason": parsed.severe_regression_reason,
                    "abstained": parsed.abstained,
                    "abstention_reason": parsed.abstention_reason,
                    "raw_response": raw_response,
                    "execution_id": stored_result["execution_id"],
                    "session_id": stored_result["session_id"],
                    "judge_provenance": stored_result["judge_provenance"],
                    "request_fingerprint": rebuilt.fingerprint,
                },
                stored_result,
                "reparsed judge result",
            )
            parsed_results.append(parsed)
            rebuilt_by_role[rebuilt.role] = rebuilt
            rebuilt_request_fingerprints.append(rebuilt.fingerprint)
        if (
            len(rebuilt_by_role) != 3
            or manifest["judge_request_fingerprints"] != rebuilt_request_fingerprints
        ):
            return False

        recomputed_panel = aggregate_judge_panel(
            [rebuilt_by_role[entry["role"]] for entry in judge_request_entries],
            parsed_results,
            recomputed_gates,
        )
        _canonical_matches(
            _panel_report_payload(recomputed_panel), panel_payload, "replayed panel report"
        )
        _canonical_matches(
            _defect_log_payload(parsed_results, panel_payload),
            defect_log_payload,
            "replayed defect log",
        )
        return True
    except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError):
        return False

