"""Offline model comparison harness (live-platform spec §2.5, §18).

Runs identical versioned fixtures against pinned candidate model slugs and
records deterministic quality, reliability, latency, and cost metrics. The
harness never runs during production processing; it executes only when an
operator invokes the ``benchmark-models`` CLI command.

Production processors always use exactly one configured model
(``llm.models.default``). This harness is for evaluation and promotion
decisions only, and its outputs are written to an operator-chosen artifact
directory, never into production publication tables.
"""

import hashlib
import html
import json
import math
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from budgets import (
    BudgetBlock,
    BudgetContext,
    mint_trusted_manual_authorization,
    trusted_manual_budget_context,
)
from llm_client import call_llm
from logging_config import get_logger

logger = get_logger("model_benchmark")

FIXTURES_ROOT = Path(__file__).resolve().parent / "tests" / "fixtures" / "model_eval"
BENCHMARK_PROCESSOR_ID = "benchmark"
REQUIRED_CASE_FIELDS = (
    "case_id",
    "suite",
    "fixture_version",
    "task",
    "prompt_version",
    "request_profile",
    "messages",
    "response_schema",
    "expectations",
)
DEFAULT_CANDIDATE_MODELS = (
    "deepseek/deepseek-v4-flash-0731",
    "openai/gpt-5.6-luna",
)

DEFAULT_SCORING_POLICY = {
    "weights": {
        "factual_evidence": 0.30,
        "reliability": 0.20,
        "usefulness": 0.15,
        "uncertainty": 0.10,
        "latency": 0.10,
        "cost": 0.10,
        "stability": 0.05,
    },
    "min_schema_valid_after_repair": 0.99,
    "max_mean_latency_ms": 20_000,
    "max_mean_cost_usd": 0.05,
}
BLIND_REVIEW_CRITERIA = (
    "Factual faithfulness",
    "Evidence discipline",
    "Observation vs interpretation distinction",
    "Usefulness to a professional trader/investor",
    "Handling of uncertainty",
    "Quality of invalidation conditions",
    "Concision and information density",
    "Absence of generic filler",
)

_SUITE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MODEL_SLUG_RE = re.compile(r"^[a-z0-9._-]+/[a-z0-9._-]+(?::[a-z0-9._-]+)?$")
_REQUIRED_MANIFEST_FIELDS = (
    "suite",
    "suite_version",
    "fixture_schema_version",
    "candidate_models",
    "case_schema",
)


class FixtureError(ValueError):
    pass


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FixtureError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FixtureError(f"{path.name} must contain a JSON object")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixtureError(f"{label} must be a non-empty string")
    return value


def _require_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FixtureError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise FixtureError(f"{label} must be at least {minimum}")
    return value


def _validate_strict_response_schema(schema: object, label: str) -> None:
    if not isinstance(schema, dict):
        return
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise FixtureError(f"{label}.properties must be an object")
        if schema.get("additionalProperties") is not False:
            raise FixtureError(f"{label}.additionalProperties must be false")
        required = schema.get("required")
        if not isinstance(required, list) or set(required) != set(properties):
            raise FixtureError(f"{label}.required must list every property")
        for name, child in properties.items():
            _validate_strict_response_schema(child, f"{label}.properties.{name}")
    elif schema_type == "array":
        _validate_strict_response_schema(schema.get("items"), f"{label}.items")


def _validate_case(case: dict, path: Path, suite: str) -> None:
    missing = [field for field in REQUIRED_CASE_FIELDS if field not in case]
    if missing:
        raise FixtureError(f"{path.name} missing fields: {missing}")
    case_id = _require_string(case["case_id"], f"{path.name}.case_id")
    if not _CASE_ID_RE.fullmatch(case_id):
        raise FixtureError(f"{path.name} has unsafe case_id: {case_id}")
    if case["suite"] != suite:
        raise FixtureError(f"{path.name} declares suite {case['suite']}")
    _require_int(case["fixture_version"], f"{path.name}.fixture_version", minimum=1)
    for field in ("task", "prompt_version"):
        _require_string(case[field], f"{path.name}.{field}")

    profile = case["request_profile"]
    if not isinstance(profile, dict):
        raise FixtureError(f"{path.name}.request_profile must be an object")
    max_tokens = _require_int(
        profile.get("max_output_tokens"),
        f"{path.name}.request_profile.max_output_tokens",
    )
    if not 1 <= max_tokens <= 4096:
        raise FixtureError(
            f"{path.name}.request_profile.max_output_tokens must be 1..4096"
        )
    temperature = profile.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise FixtureError(f"{path.name}.request_profile.temperature must be numeric")
    if not 0 <= temperature <= 2:
        raise FixtureError(f"{path.name}.request_profile.temperature must be 0..2")

    messages = case["messages"]
    if not isinstance(messages, list) or not messages:
        raise FixtureError(f"{path.name}.messages must be a non-empty array")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise FixtureError(f"{path.name}.messages[{index}] must be an object")
        _require_string(message.get("role"), f"{path.name}.messages[{index}].role")
        _require_string(
            message.get("content"), f"{path.name}.messages[{index}].content"
        )

    schema = case["response_schema"]
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise FixtureError(f"{path.name}.response_schema must be an object schema")
    _validate_strict_response_schema(schema, f"{path.name}.response_schema")

    expectations = case["expectations"]
    if not isinstance(expectations, dict):
        raise FixtureError(f"{path.name}.expectations must be an object")
    for field in ("required_fields", "allowed_evidence_ids", "forbidden_phrases"):
        if field not in expectations:
            raise FixtureError(f"{path.name}.expectations missing field: {field}")
        values = expectations[field]
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item.strip() for item in values
        ):
            raise FixtureError(
                f"{path.name}.expectations.{field} must be a string array"
            )
    for field in ("allowed_numbers", "contradiction_checks"):
        if field in expectations and not isinstance(expectations[field], list):
            raise FixtureError(f"{path.name}.expectations.{field} must be an array")


def load_suite(suite: str, fixtures_dir: Path | None = None) -> list[dict]:
    """Load and validate every case in a versioned fixture suite."""
    if not isinstance(suite, str) or not _SUITE_RE.fullmatch(suite):
        raise FixtureError(f"invalid fixture suite name: {suite!r}")
    root = (fixtures_dir or FIXTURES_ROOT).resolve()
    suite_dir = (root / suite).resolve()
    if not suite_dir.is_relative_to(root):
        raise FixtureError(f"fixture suite escapes fixture root: {suite}")
    manifest_path = suite_dir / "manifest.json"
    if not suite_dir.is_dir():
        raise FixtureError(f"fixture suite not found: {suite}")
    if not manifest_path.is_file():
        raise FixtureError(f"fixture suite {suite} is missing manifest.json")
    manifest = _read_json(manifest_path)
    missing_manifest = [
        field for field in _REQUIRED_MANIFEST_FIELDS if field not in manifest
    ]
    if missing_manifest:
        raise FixtureError(f"manifest.json missing fields: {missing_manifest}")
    if manifest["suite"] != suite:
        raise FixtureError(f"manifest suite mismatch in {manifest_path}")
    _require_int(manifest["suite_version"], "manifest.suite_version", minimum=1)
    _require_int(
        manifest["fixture_schema_version"],
        "manifest.fixture_schema_version",
        minimum=1,
    )
    candidates = manifest["candidate_models"]
    if not isinstance(candidates, list) or not candidates:
        raise FixtureError("manifest.candidate_models must be a non-empty array")

    for model in candidates:
        if not isinstance(model, str) or not _MODEL_SLUG_RE.fullmatch(model):
            raise FixtureError(f"manifest has invalid model slug: {model!r}")
    if len(set(candidates)) != len(candidates):
        raise FixtureError("manifest.candidate_models contains duplicates")
    case_schema = manifest["case_schema"]
    if not isinstance(case_schema, dict) or not case_schema:
        raise FixtureError("manifest.case_schema must be a non-empty object")
    cases_dir = suite_dir / "cases"
    if not cases_dir.is_dir():
        raise FixtureError(f"fixture suite {suite} is missing cases/")
    cases = []
    for path in sorted(cases_dir.glob("*.json")):
        case = _read_json(path)
        _validate_case(case, path, suite)
        cases.append(case)
    if not cases:
        raise FixtureError(f"suite {suite} contains no cases")
    seen = set()
    for case in cases:
        if case["case_id"] in seen:
            raise FixtureError(f"duplicate case_id {case['case_id']}")
        seen.add(case["case_id"])
    return cases


def build_request_body(
    case: dict, model: str, *, include_temperature: bool = True
) -> dict:
    """Fair request body: identical across models except the pinned slug."""
    profile = case["request_profile"]
    body = {
        "model": model,
        "messages": case["messages"],
        "max_output_tokens": int(profile.get("max_output_tokens", 1024)),
        "response_schema": case["response_schema"],
    }
    if include_temperature:
        body["temperature"] = float(profile.get("temperature", 0.2))
    return body


def _response_schema_envelope(case: dict) -> dict:
    """Convert a fixture's bare JSON Schema to the provider contract."""
    schema_name = re.sub(r"[^A-Za-z0-9_-]", "_", case["case_id"])[:64]
    return {
        "name": schema_name,
        "strict": True,
        "schema": case["response_schema"],
    }


def _parse_output(content: str) -> dict | None:
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _field_path_exists(payload: dict, path: str) -> bool:
    node: object = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _collect_evidence_ids(payload: dict) -> list[str]:
    ids: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith("evidence_ids") or key == "evidence_ids":
                    if isinstance(value, list):
                        ids.extend(str(item) for item in value)
                    elif isinstance(value, str):
                        ids.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return ids


def _evidence_text(case: dict) -> str:
    return "\n".join(
        message.get("content", "")
        for message in case.get("messages", [])
        if message.get("role") == "user"
    )


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s?%?")


def _unsupported_numbers(case: dict, raw_content: str) -> list[str]:
    expectations = case.get("expectations", {})
    allowed = set(expectations.get("allowed_numbers", []))
    evidence = _evidence_text(case)
    hits = []
    for token in _NUMBER_RE.findall(raw_content or ""):
        normalized = token.strip()
        if normalized in allowed or token in evidence or normalized in evidence:
            continue
        if not normalized.endswith("%") and 0.0 <= float(normalized) <= 1.0:
            continue
        hits.append(normalized)
    return sorted(set(hits))


def _contradiction_hits(case: dict, raw_content: str) -> list[str]:
    expectations = case.get("expectations", {})
    lowered = (raw_content or "").lower()
    hits = []
    for check in expectations.get("contradiction_checks", []):
        if not isinstance(check, dict):
            continue
        phrase = str(check.get("phrase") or "").lower()
        marker = str(check.get("evidence_marker") or "").lower()
        if phrase and marker and phrase in lowered and marker in lowered:
            hits.append(phrase)
    return hits


def evaluate_output(case: dict, parsed: dict | None, raw_content: str) -> dict:
    """Deterministic quality checks shared by every candidate model."""
    expectations = case.get("expectations", {})
    metrics = {
        "schema_valid": parsed is not None,
        "missing_fields": [],
        "invalid_evidence_ids": [],
        "forbidden_phrase_hits": [],
        "unsupported_numerical_claims": [],
        "contradiction_hits": [],
        "output_length_chars": len(raw_content or ""),
    }
    if parsed is None:
        metrics["missing_fields"] = list(expectations.get("required_fields", []))
        return metrics
    for field in expectations.get("required_fields", []):
        if not _field_path_exists(parsed, field):
            metrics["missing_fields"].append(field)
    allowed = expectations.get("allowed_evidence_ids")
    if allowed is not None:
        allowed_set = set(allowed)
        metrics["invalid_evidence_ids"] = sorted(
            {
                evidence_id
                for evidence_id in _collect_evidence_ids(parsed)
                if evidence_id not in allowed_set
            }
        )
    lowered = (raw_content or "").lower()
    for phrase in expectations.get("forbidden_phrases", []):
        pattern = rf"(?<!\w){re.escape(phrase.lower())}(?!\w)"
        if re.search(pattern, lowered):
            metrics["forbidden_phrase_hits"].append(phrase)
    metrics["unsupported_numerical_claims"] = _unsupported_numbers(case, raw_content)
    metrics["contradiction_hits"] = _contradiction_hits(case, raw_content)
    return metrics


def run_case_once(
    config: dict,
    case: dict,
    model: str,
    *,
    budget_context: BudgetContext,
    include_temperature: bool = True,
) -> dict:
    """One attempt against one model; raises BudgetBlock when blocked."""
    started = time.monotonic()
    result = call_llm(
        prompt=case["messages"][-1]["content"],
        messages=case["messages"],
        model=model,
        config=config,
        processor_id=BENCHMARK_PROCESSOR_ID,
        temperature=float(case["request_profile"].get("temperature", 0.2)),
        include_temperature=include_temperature,
        max_output_tokens=int(case["request_profile"].get("max_output_tokens", 1024)),
        structured_response=True,
        response_schema=_response_schema_envelope(case),
        correlation_id=str(uuid.uuid4()),
        budget_context=budget_context,
    )
    parsed = _parse_output(result.get("content"))
    evaluation = evaluate_output(case, parsed, result.get("content") or "")
    reported_duration = result.get("duration_ms")
    if isinstance(reported_duration, (int, float)) and not isinstance(
        reported_duration, bool
    ):
        latency_ms = max(0, int(reported_duration))
    else:
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
    return {
        "http_ok": True,
        "latency_ms": latency_ms,
        "time_to_first_token_ms": result.get("time_to_first_token_ms"),
        "requested_model": result.get("requested_model", model),
        "resolved_model": result.get("model"),
        "provider": result.get("provider"),
        "generation_id": result.get("generation_id"),
        "retry_count": result.get("retry_count", 0),
        "tokens_input": result.get("tokens_input", 0),
        "tokens_output": result.get("tokens_output", 0),
        "tokens_reasoning": result.get("tokens_reasoning", 0),
        "tokens_cached": result.get("tokens_cached", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "parsed": parsed,
        **evaluation,
    }


def run_case_with_repair(
    config: dict,
    case: dict,
    model: str,
    *,
    budget_context: BudgetContext,
    include_temperature: bool = True,
) -> dict:
    """Bounded retry policy: one strict attempt, one schema-repair attempt."""
    first = run_case_once(
        config,
        case,
        model,
        budget_context=budget_context,
        include_temperature=include_temperature,
    )
    first_pass_valid = first["schema_valid"] and not first["missing_fields"]
    if first_pass_valid:
        first["schema_valid_first_pass"] = True
        first["schema_valid_after_repair"] = True
        first["attempts_used"] = 1
        first["repair_used"] = False
        return first
    first["schema_valid_first_pass"] = False
    repair_case = dict(case)
    repair_case["messages"] = list(case["messages"]) + [
        {
            "role": "user",
            "content": (
                "Your previous response failed schema validation (missing fields: "
                f"{', '.join(first['missing_fields']) or 'unparseable JSON'}). "
                "Respond again with the exact required JSON schema and nothing else."
            ),
        }
    ]
    second = run_case_once(
        config,
        repair_case,
        model,
        budget_context=budget_context,
        include_temperature=include_temperature,
    )
    second["schema_valid_first_pass"] = False
    second["schema_valid_after_repair"] = bool(
        second["schema_valid"] and not second["missing_fields"]
    )
    second["attempts_used"] = 2
    second["repair_used"] = True
    second["first_attempt"] = {
        key: first.get(key)
        for key in (
            "latency_ms",
            "tokens_input",
            "tokens_output",
            "tokens_reasoning",
            "tokens_cached",
            "cost_usd",
            "retry_count",
            "schema_valid",
            "missing_fields",
        )
    }
    # The logical run includes both provider attempts. Keep the final response
    # for quality evaluation, but account for all spend and latency.
    for key in (
        "latency_ms",
        "tokens_input",
        "tokens_output",
        "tokens_reasoning",
        "tokens_cached",
        "cost_usd",
        "retry_count",
    ):
        second[key] = first.get(key, 0) + second.get(key, 0)
    return second


def summarize_case_runs(runs: list[dict]) -> dict:
    """Aggregate metrics for one (case, model) across repeated runs."""
    total = len(runs)
    if total == 0:
        return {"runs": 0}

    def rate(predicate) -> float:
        return sum(1 for run in runs if predicate(run)) / total

    latencies = sorted(max(0, run.get("latency_ms", 0)) for run in runs)
    p95_index = min(total - 1, max(0, math.ceil(total * 0.95) - 1))
    return {
        "runs": total,
        "http_success_rate": rate(lambda run: bool(run.get("http_ok"))),
        "schema_valid_first_pass_rate": rate(
            lambda run: bool(run.get("schema_valid_first_pass"))
        ),
        "schema_valid_after_repair_rate": rate(
            lambda run: bool(run.get("schema_valid_after_repair"))
        ),
        "repair_rate": rate(lambda run: run.get("attempts_used", 1) > 1),
        "evidence_valid_rate": rate(
            lambda run: bool(run.get("http_ok"))
            and bool(run.get("schema_valid_after_repair", run.get("schema_valid")))
            and not run.get("invalid_evidence_ids")
        ),
        "completeness_rate": rate(
            lambda run: bool(run.get("http_ok"))
            and bool(run.get("schema_valid_after_repair", run.get("schema_valid")))
            and not run.get("missing_fields")
        ),
        "invalid_evidence_ids_total": sum(
            len(run.get("invalid_evidence_ids", [])) for run in runs
        ),
        "evidence_fabrication_runs": sum(
            bool(run.get("invalid_evidence_ids")) for run in runs
        ),
        "policy_violations": sum(
            len(run.get("forbidden_phrase_hits", [])) for run in runs
        ),
        "policy_violation_runs": sum(
            bool(run.get("forbidden_phrase_hits")) for run in runs
        ),
        "unsupported_numerical_claims": sum(
            len(run.get("unsupported_numerical_claims", [])) for run in runs
        ),
        "contradiction_hits": sum(
            len(run.get("contradiction_hits", [])) for run in runs
        ),
        "output_stability": _output_stability(runs),
        "mean_latency_ms": sum(latencies) / total,
        "p95_latency_ms": latencies[p95_index],
        "mean_cost_usd": sum(run.get("cost_usd", 0.0) for run in runs) / total,
        "mean_output_length": sum(run.get("output_length_chars", 0) for run in runs)
        / total,
        "total_attempts": sum(run.get("attempts_used", 1) for run in runs),
        "total_tokens_input": sum(run.get("tokens_input", 0) for run in runs),
        "total_tokens_output": sum(run.get("tokens_output", 0) for run in runs),
        "total_tokens_reasoning": sum(run.get("tokens_reasoning", 0) for run in runs),
        "total_tokens_cached": sum(run.get("tokens_cached", 0) for run in runs),
    }


def _canonical_fingerprint(run: dict) -> str | None:
    parsed = run.get("parsed")
    if not isinstance(parsed, dict):
        return None
    return hashlib.sha256(
        json.dumps(parsed, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _output_stability(runs: list[dict]) -> float:
    fingerprints = [
        fingerprint for fingerprint in map(_canonical_fingerprint, runs) if fingerprint
    ]
    if not fingerprints:
        return 0.0
    counts: dict[str, int] = {}
    for fingerprint in fingerprints:
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
    return max(counts.values()) / len(fingerprints)


def _artifact_model_component(model: str) -> str:
    """Map a validated provider/model slug to one safe directory component."""
    if not isinstance(model, str) or not _MODEL_SLUG_RE.fullmatch(model):
        raise FixtureError(f"invalid model slug: {model!r}")
    return model.replace("/", "__")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _human_score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FixtureError("blind review scores must be numbers from 1 to 5")
    score = float(value)
    if score < 1 or score > 5:
        raise FixtureError("blind review scores must be numbers from 1 to 5")
    return (score - 1.0) / 4.0


def score_models(
    summary: dict,
    policy: dict | None = None,
    blind_review: dict | None = None,
) -> dict:
    """Weighted decision score plus hard disqualifiers (spec §18.7)."""
    active = {**DEFAULT_SCORING_POLICY, **(policy or {})}
    weights = active["weights"]
    blind_models = (blind_review or {}).get("models", {})
    expected_models = set(summary.get("models", {}))
    review_complete = (
        (blind_review or {}).get("complete") is True
        and bool(expected_models)
        and expected_models <= set(blind_models)
    )
    scores: dict[str, float] = {}
    disqualified: dict[str, list[str]] = {}
    for model, metrics in summary.get("models", {}).items():
        runs = metrics.get("runs", 0)
        if not runs:
            disqualified.setdefault(model, []).append("no_runs")
            continue
        reasons = []
        after_repair = metrics.get("schema_valid_after_repair_rate", 0.0)
        if (
            summary.get("suite") == "core"
            and after_repair < active["min_schema_valid_after_repair"]
        ):
            reasons.append("schema_valid_after_repair_below_threshold")
        fabrication_runs = metrics.get(
            "evidence_fabrication_runs",
            metrics.get("invalid_evidence_ids_total", 0),
        )
        if fabrication_runs > 1:
            reasons.append("persistent_evidence_fabrication")
        violation_runs = metrics.get(
            "policy_violation_runs", metrics.get("policy_violations", 0)
        )
        if violation_runs > 1:
            reasons.append("repeated_policy_violations")
        if metrics.get("mean_latency_ms", 0.0) > active["max_mean_latency_ms"]:
            reasons.append("latency_incompatible")
        if metrics.get("mean_cost_usd", 0.0) > active["max_mean_cost_usd"]:
            reasons.append("cost_incompatible")
        if reasons:
            disqualified[model] = reasons
        reliability = metrics.get("http_success_rate", 0.0) * 0.5 + after_repair * 0.5
        review = blind_models.get(model, {})
        criterion_means = review.get("criteria_mean", [])
        if len(criterion_means) == len(BLIND_REVIEW_CRITERIA):
            normalized = [_human_score(value) for value in criterion_means]
            factual = (normalized[0] + normalized[1]) / 2
            usefulness = normalized[3]
            uncertainty = normalized[4]
        else:
            factual = (
                metrics.get("evidence_valid_rate", 0.0) * 0.5
                + metrics.get("completeness_rate", 0.0) * 0.3
                + (
                    1.0
                    if not metrics.get("unsupported_numerical_claims")
                    and not metrics.get("contradiction_hits")
                    else 0.0
                )
                * 0.2
            )
            usefulness = metrics.get("completeness_rate", 0.0)
            uncertainty = metrics.get("evidence_valid_rate", 0.0)
        latency_score = max(
            0.0,
            1.0 - metrics.get("mean_latency_ms", 0.0) / active["max_mean_latency_ms"],
        )
        cost_score = max(
            0.0,
            1.0 - metrics.get("mean_cost_usd", 0.0) / active["max_mean_cost_usd"],
        )
        scores[model] = round(
            weights["factual_evidence"] * factual
            + weights["reliability"] * reliability
            + weights["usefulness"] * usefulness
            + weights["uncertainty"] * uncertainty
            + weights["latency"] * latency_score
            + weights["cost"] * cost_score
            + weights["stability"] * metrics.get("output_stability", 0.0),
            4,
        )
    eligible = {
        model: score for model, score in scores.items() if model not in disqualified
    }
    recommended = (
        max(eligible, key=eligible.get) if review_complete and eligible else None
    )
    return {
        "policy": active,
        "scores": scores,
        "disqualified": disqualified,
        "blind_review_complete": review_complete,
        "recommended": recommended,
    }


def render_blind_review(
    summary: dict,
    raw_dir: Path,
    *,
    key_path: Path | None = None,
) -> str:
    """Model-anonymized review page; order randomized per case."""
    import random

    rng = random.Random(summary.get("run_id", "blind"))
    models_by_component = {
        _artifact_model_component(model): model for model in summary.get("models", {})
    }
    cases: dict[str, list[dict]] = {}
    review_key = {
        "run_id": summary.get("run_id"),
        "criteria": list(BLIND_REVIEW_CRITERIA),
        "cases": {},
    }
    if raw_dir.is_dir():
        for case_dir in sorted(raw_dir.iterdir()):
            if not case_dir.is_dir():
                continue
            entries = []
            for model_dir in sorted(case_dir.iterdir()):
                if not model_dir.is_dir():
                    continue
                actual_model = models_by_component.get(model_dir.name)
                if actual_model is None:
                    raise FixtureError(
                        f"raw artifact has unknown model directory: {model_dir.name}"
                    )
                outputs = []
                for run_file in sorted(model_dir.glob("run-*.json")):
                    payload = _read_json(run_file)
                    parsed = payload.get("parsed")
                    outputs.append(
                        parsed if parsed is not None else payload.get("error_type", "")
                    )
                entries.append(
                    {
                        "model": model_dir.name,
                        "actual_model": actual_model,
                        "outputs": outputs,
                    }
                )
            rng.shuffle(entries)
            for index, entry in enumerate(entries):
                entry["blind_label"] = f"Model {chr(65 + index)}"
            cases[case_dir.name] = entries
            review_key["cases"][case_dir.name] = {
                entry["blind_label"]: entry["actual_model"] for entry in entries
            }
    if key_path is not None:
        _write_json(key_path, review_key)
    criteria_html = "".join(
        f"<label>{html.escape(criterion)} "
        f"<select data-criterion='{index}'><option value=''>—</option>"
        + "".join(f"<option value='{value}'>{value}</option>" for value in range(1, 6))
        + "</select></label>"
        for index, criterion in enumerate(BLIND_REVIEW_CRITERIA)
    )
    sections = []
    for case_id, entries in cases.items():
        identities = {
            identity
            for entry in entries
            for identity in (
                entry["model"],
                entry["actual_model"],
            )
        }
        blocks = []
        for entry in entries:
            rendered_outputs = []
            for output in entry["outputs"]:
                serialized = json.dumps(output, indent=2, default=str)[:4000]
                for identity in identities:
                    serialized = serialized.replace(
                        identity, "[model identity redacted]"
                    )
                rendered_outputs.append(f"<pre>{html.escape(serialized)}</pre>")
            rendered = "".join(rendered_outputs)
            blocks.append(
                "<div class='blind-output' "
                f"data-label='{html.escape(entry['blind_label'])}'>"
                f"<h3>{html.escape(entry['blind_label'])}</h3>{rendered}"
                f"<div class='scores'>{criteria_html}</div>"
                "<textarea placeholder='reviewer rationale'></textarea></div>"
            )
        sections.append(
            f"<section data-case='{html.escape(case_id)}'><h2>{html.escape(case_id)}</h2>"
            f"{''.join(blocks)}</section>"
        )
    run_id_json = json.dumps(summary.get("run_id", ""))
    criteria_json = json.dumps(list(BLIND_REVIEW_CRITERIA))
    script = (
        "<button id='save-review' type='button'>Download completed review JSON</button>"
        "<script>"
        f"const runId={run_id_json};const criteria={criteria_json};"
        "document.getElementById('save-review').addEventListener('click',()=>{"
        "const entries=[];let invalid=false;"
        "document.querySelectorAll('.blind-output').forEach(block=>{"
        "const scores=[...block.querySelectorAll('select')].map(s=>Number(s.value));"
        "const rationale=block.querySelector('textarea').value.trim();"
        "if(scores.some(s=>!Number.isInteger(s)||s<1||s>5)||!rationale){invalid=true;}"
        "entries.push({case_id:block.closest('section').dataset.case,"
        "blind_label:block.dataset.label,scores,rationale});});"
        "if(invalid){alert('Score every criterion and provide rationale for every output.');return;}"
        "const payload={run_id:runId,criteria,entries};"
        "localStorage.setItem('model-benchmark-review-'+runId,JSON.stringify(payload));"
        "const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});"
        "const link=document.createElement('a');link.href=URL.createObjectURL(blob);"
        "link.download='blind-review-scores.json';link.click();URL.revokeObjectURL(link.href);"
        "});</script>"
    )
    return (
        "<!doctype html><meta charset='utf-8'><title>Blind review</title>"
        "<style>body{font-family:sans-serif;background:#111;color:#eee}"
        ".blind-output{border:1px solid #333;padding:8px;margin:8px 0}"
        "pre{white-space:pre-wrap}label{display:block;margin:.35rem 0}"
        "textarea{width:100%;min-height:6rem}</style>"
        f"<h1>Blind review — {html.escape(str(summary.get('run_id', '')))}</h1>"
        "<p>Model identities are anonymized and order randomized per case. "
        "Score 1-5 per criterion and record rationale.</p>" + "".join(sections) + script
    )


def apply_blind_review_scores(
    artifact_dir: str | Path,
    review_path: str | Path,
) -> dict:
    """Validate a complete blind review, map labels, and finalize the decision."""
    output = Path(artifact_dir)
    summary = _read_json(output / "summary.json")
    manifest = _read_json(output / "manifest.json")
    key = _read_json(output / "blind-review-key.json")
    review = _read_json(Path(review_path))
    if review.get("run_id") != summary.get("run_id") or key.get(
        "run_id"
    ) != summary.get("run_id"):
        raise FixtureError("blind review run_id does not match the artifact")
    if review.get("criteria") != list(BLIND_REVIEW_CRITERIA):
        raise FixtureError("blind review criteria do not match the benchmark rubric")
    entries = review.get("entries")
    if not isinstance(entries, list):
        raise FixtureError("blind review entries must be an array")
    expected = {
        (case_id, label)
        for case_id, labels in key.get("cases", {}).items()
        for label in labels
    }
    seen: set[tuple[str, str]] = set()
    model_scores: dict[str, list[list[float]]] = {}
    rationales: dict[str, list[dict]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise FixtureError("blind review entry must be an object")
        identity = (entry.get("case_id"), entry.get("blind_label"))
        if identity not in expected or identity in seen:
            raise FixtureError("blind review has an unknown or duplicate candidate")
        seen.add(identity)
        rationale = entry.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise FixtureError("blind review rationale is required")
        scores = entry.get("scores")
        if not isinstance(scores, list) or len(scores) != len(BLIND_REVIEW_CRITERIA):
            raise FixtureError("blind review must score all eight criteria")
        validated = []
        for score in scores:
            _human_score(score)
            validated.append(float(score))
        case_id, label = identity
        model = key["cases"][case_id][label]
        model_scores.setdefault(model, []).append(validated)
        rationales.setdefault(model, []).append(
            {"case_id": case_id, "blind_label": label, "rationale": rationale.strip()}
        )
    if seen != expected:
        raise FixtureError("blind review is incomplete")
    reviewed_models = {}
    for model, rows in model_scores.items():
        reviewed_models[model] = {
            "case_count": len(rows),
            "criteria_mean": [
                round(sum(row[index] for row in rows) / len(rows), 4)
                for index in range(len(BLIND_REVIEW_CRITERIA))
            ],
            "rationales": rationales[model],
        }
    blind_review = {
        "complete": True,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "criteria": list(BLIND_REVIEW_CRITERIA),
        "models": reviewed_models,
    }
    summary["blind_review"] = blind_review
    summary["decision"] = score_models(summary, blind_review=blind_review)
    _write_json(output / "blind-review-scores.json", review)
    _write_json(output / "summary.json", summary)
    (output / "summary.md").write_text(_render_summary_markdown(summary, manifest))
    return summary


def run_benchmark(
    config: dict,
    *,
    models: list[str],
    suite: str = "core",
    runs: int = 3,
    output_dir: str | Path,
    force: bool = False,
    dry_run: bool = False,
    include_temperature: bool = True,
) -> dict:
    """Execute the evaluation harness and write the artifact bundle."""
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
        raise FixtureError("runs must be an integer of at least 1")
    if not isinstance(models, list) or not models:
        raise FixtureError("at least one model slug is required")
    if any(not isinstance(model, str) for model in models):
        raise FixtureError("model slugs must be strings")
    if len(set(models)) != len(models):
        raise FixtureError("duplicate model slugs")
    if not isinstance(include_temperature, bool):
        raise FixtureError("include_temperature must be a boolean")
    artifact_models = {}
    for model in models:
        artifact_models[model] = _artifact_model_component(model)
    cases = load_suite(suite)
    output = Path(output_dir)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    run_id = f"{suite}-{timestamp}"
    budget_context = (
        trusted_manual_budget_context(
            force=True,
            manual_authorized=True,
            authorization=mint_trusted_manual_authorization(),
        )
        if force
        else BudgetContext()
    )

    manifest = {
        "run_id": run_id,
        "suite": suite,
        "models": list(models),
        "runs_per_case": runs,
        "dry_run": dry_run,
        "force_budget": force,
        "include_temperature": include_temperature,
        "started_at": datetime.now(UTC).isoformat(),
        "cases": [
            {
                "case_id": case["case_id"],
                "fixture_version": case["fixture_version"],
                "task": case["task"],
                "prompt_version": case["prompt_version"],
            }
            for case in cases
        ],
    }
    if dry_run:
        for case in cases:
            for model in models:
                _write_json(
                    output
                    / "raw"
                    / case["case_id"]
                    / artifact_models[model]
                    / "request.json",
                    build_request_body(
                        case, model, include_temperature=include_temperature
                    ),
                )
        _write_json(output / "manifest.json", manifest)
        logger.info("model_benchmark_dry_run", run_id=run_id, cases=len(cases))
        return {"run_id": run_id, "dry_run": True, "cases": len(cases)}

    results: list[dict] = []
    per_model_runs: dict[str, list[dict]] = {model: [] for model in models}
    for case in cases:
        for model in models:
            model_runs = []
            for run_index in range(1, runs + 1):
                try:
                    run_result = run_case_with_repair(
                        config,
                        case,
                        model,
                        budget_context=budget_context,
                        include_temperature=include_temperature,
                    )
                except BudgetBlock as exc:
                    run_result = {
                        "http_ok": False,
                        "suppressed_budget": True,
                        "blocked_code": exc.code,
                        "latency_ms": 0,
                        "tokens_input": 0,
                        "tokens_output": 0,
                        "cost_usd": 0.0,
                    }
                except Exception as exc:
                    run_result = {
                        "http_ok": False,
                        "error_type": type(exc).__name__,
                        "latency_ms": 0,
                        "tokens_input": 0,
                        "tokens_output": 0,
                        "cost_usd": 0.0,
                    }
                run_result["case_id"] = case["case_id"]
                run_result["model"] = model
                run_result["run_index"] = run_index
                run_result["request_body"] = build_request_body(
                    case, model, include_temperature=include_temperature
                )
                model_runs.append(run_result)
                results.append(
                    {key: value for key, value in run_result.items() if key != "parsed"}
                )
                raw = dict(run_result)
                _write_json(
                    output
                    / "raw"
                    / case["case_id"]
                    / artifact_models[model]
                    / f"run-{run_index}.json",
                    raw,
                )
            per_model_runs[model].extend(model_runs)
            logger.info(
                "model_benchmark_case_completed",
                run_id=run_id,
                case_id=case["case_id"],
                model=model,
                runs=len(model_runs),
            )

    summary = {
        "run_id": run_id,
        "suite": suite,
        "models": {
            model: summarize_case_runs(model_runs)
            for model, model_runs in per_model_runs.items()
        },
        "completed_at": datetime.now(UTC).isoformat(),
    }
    summary["decision"] = score_models(summary)
    _write_json(output / "manifest.json", manifest)
    _write_json(output / "summary.json", summary)
    with (output / "case-results.jsonl").open("w") as handle:
        for record in results:
            handle.write(json.dumps(record, default=str) + "\n")
    (output / "summary.md").write_text(_render_summary_markdown(summary, manifest))
    (output / "blind-review.html").write_text(
        render_blind_review(
            summary,
            output / "raw",
            key_path=output / "blind-review-key.json",
        )
    )
    logger.info("model_benchmark_completed", run_id=run_id, results=len(results))
    return summary


def _render_summary_markdown(summary: dict, manifest: dict) -> str:
    lines = [
        f"# Model benchmark — {summary['run_id']}",
        "",
        f"Suite: `{manifest['suite']}` · runs per case: {manifest['runs_per_case']}",
        "",
        "| Model | First-pass valid | After repair | Evidence valid | Mean latency | Mean cost |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model, metrics in summary["models"].items():
        lines.append(
            "| {model} | {first:.0%} | {repair:.0%} | {evidence:.0%} "
            "| {latency:.0f} ms | ${cost:.4f} |".format(
                model=model,
                first=metrics.get("schema_valid_first_pass_rate", 0.0),
                repair=metrics.get("schema_valid_after_repair_rate", 0.0),
                evidence=metrics.get("evidence_valid_rate", 0.0),
                latency=metrics.get("mean_latency_ms", 0.0),
                cost=metrics.get("mean_cost_usd", 0.0),
            )
        )
    decision = summary.get("decision", {})
    if decision:
        lines.extend(["", "## Decision score", ""])
        review_status = (
            "complete" if decision.get("blind_review_complete") else "pending"
        )
        lines.append(f"- blind review: {review_status}")
        for model, score in sorted(decision.get("scores", {}).items()):
            status = (
                "disqualified: "
                + ", ".join(decision.get("disqualified", {}).get(model, []))
                if model in decision.get("disqualified", {})
                else "eligible"
            )
            lines.append(f"- {model}: {score:.3f} ({status})")
        recommended = decision.get("recommended")
        lines.append(f"- recommended: {recommended or 'none eligible'}")
    lines.extend(
        [
            "",
            "Promotion requires review of this artifact and an ADR record; the",
            "production configuration keeps exactly one model slug.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_model_list(raw: str) -> list[str]:
    if not isinstance(raw, str):
        raise FixtureError("--models must be a comma-separated string")
    models = [item.strip() for item in raw.split(",") if item.strip()]
    if not models:
        raise FixtureError("at least one model slug is required")
    for model in models:
        if not _MODEL_SLUG_RE.fullmatch(model):
            raise FixtureError(f"invalid model slug: {model}")
    if len(set(models)) != len(models):
        raise FixtureError("duplicate model slugs")
    return models
