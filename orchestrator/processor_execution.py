"""Processor execution, reuse decisions, budget policy, and telemetry.

The public orchestrator module re-exports these helpers for backwards
compatibility. Runtime dependencies are resolved through that facade so tests
and integrations retaining the historical patch seams continue to work.
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text

from budgets import BudgetBlock, BudgetContext
from errors import InvalidSourceData, PersistenceError, classify_error
from llm_client import LLMStageFailure
from logging_config import get_logger
from processors.base import canonical_fingerprint

logger = get_logger("orchestrator.processor_execution")


def _facade():
    import orchestrator

    return orchestrator


def _dependency(name: str, fallback):
    """Read a dependency from the compatibility facade at call time."""
    return getattr(_facade(), name, fallback)


def _get_session(config: dict):
    return _dependency("get_session", None)(config)


def _resolved_config(config: dict | None) -> dict:
    if config is not None:
        return config
    from config_loader import load_config

    return load_config()


def build_processor_fingerprint(processor, config: dict) -> str | None:
    """Hash bounded processor markers plus explicit prompt/model/code versions."""
    input_builder = getattr(processor, "get_fingerprint_inputs", None)
    schema_version = getattr(processor, "PROCESSOR_SCHEMA_VERSION", None)
    if not callable(input_builder) or schema_version is None:
        return None
    from llm_client import resolve_model

    fingerprint = _dependency("canonical_fingerprint", canonical_fingerprint)
    payload = {
        "processor_id": processor.processor_id,
        "processor_schema_version": str(schema_version),
        "prompt_version": processor.get_prompt_version(),
        "prompt_identity": processor.get_prompt_identity(config),
        "model": resolve_model(config, processor_id=processor.processor_id),
        "inputs": input_builder(config),
    }
    return fingerprint(payload)


def _find_reusable_processor_output(
    processor_id: str, fingerprint: str, config: dict
) -> dict | None:
    """Find a prior successful output; failed or blocked rows are never reusable."""
    sql = text(
        "SELECT output_id, completed_at FROM processing_log "
        "WHERE processor = :processor AND input_fingerprint = :fingerprint "
        "AND status = 'success' AND output_id IS NOT NULL "
        "ORDER BY completed_at DESC LIMIT 1"
    )
    with _get_session(config) as session:
        row = session.execute(
            sql, {"processor": processor_id, "fingerprint": fingerprint}
        ).fetchone()
    return dict(row._mapping) if row is not None else None


def _resolve_and_run_processors(
    config: dict,
    correlation_id: str,
    successful_collectors: set[str],
    progress_callback=None,
    budget_context: BudgetContext | None = None,
    force: bool = False,
    analyze_existing_data: bool = False,
) -> dict:
    """Run enabled processors in dependency order, isolating stage outcomes."""
    get_all_processors = _dependency("get_all_processors", None)
    get_all_collectors = _dependency("get_all_collectors", None)
    run_processor_impl = globals()["run_processor"]
    run_processor = _dependency("run_processor", run_processor_impl)
    all_processors = get_all_processors()
    collector_dependencies = (
        set(get_all_collectors()) if analyze_existing_data else set()
    )
    processor_results: dict[str, dict] = {}
    successful_processors: set[str] = set()
    remaining = {}

    for processor_id, processor in all_processors.items():
        processor_config = config.get("processors", {}).get(processor_id, {})
        if not processor_config.get("enabled", False):
            continue
        remaining[processor_id] = processor

    for _ in range(len(remaining) + 1):
        if not remaining:
            break
        this_pass = {}
        for pid, proc in remaining.items():
            depends_on = proc.get_depends_on()
            if all(
                dep in successful_collectors
                or dep in successful_processors
                or (analyze_existing_data and dep in collector_dependencies)
                for dep in depends_on
            ):
                this_pass[pid] = proc

        if not this_pass:
            for pid, proc in remaining.items():
                depends_on = proc.get_depends_on()
                logger.warning(
                    "processor_dependencies_not_met",
                    action="run_full_cycle",
                    processor=pid,
                    depends_on=depends_on,
                    successful_collectors=list(successful_collectors),
                    successful_processors=list(successful_processors),
                    correlation_id=correlation_id,
                )
                reason = f"Dependencies not met: {', '.join(depends_on)}"
                if progress_callback:
                    progress_callback(pid, "processor", "skipped", {"error": reason})
                processor_results[pid] = {
                    "processor": pid,
                    "status": "skipped",
                    "reason": reason,
                    "correlation_id": correlation_id,
                }
            break

        for pid in this_pass:
            del remaining[pid]
        for pid in this_pass:
            if progress_callback:
                progress_callback(pid, "processor", "running")
            result = run_processor(
                pid,
                config=config,
                correlation_id=correlation_id,
                manage_lifecycle=False,
                budget_context=budget_context,
                force=force,
            )
            processor_results[pid] = result
            if progress_callback:
                progress_callback(pid, "processor", result["status"], result)
            if result["status"] in ("success", "partial") or (
                result["status"] == "skipped" and result.get("reusable_output", False)
            ):
                successful_processors.add(pid)

    return processor_results


def _normalize_processing_log(raw_processing_log) -> dict:
    if not isinstance(raw_processing_log, dict):
        return {}
    tokens_input = raw_processing_log.get("tokens_input", 0)
    if (
        not isinstance(tokens_input, int)
        or isinstance(tokens_input, bool)
        or tokens_input < 0
    ):
        tokens_input = 0
    tokens_output = raw_processing_log.get("tokens_output", 0)
    if (
        not isinstance(tokens_output, int)
        or isinstance(tokens_output, bool)
        or tokens_output < 0
    ):
        tokens_output = 0
    cost_usd = raw_processing_log.get("cost_usd", 0.0)
    if (
        not isinstance(cost_usd, (int, float))
        or isinstance(cost_usd, bool)
        or not math.isfinite(cost_usd)
        or cost_usd < 0
    ):
        cost_usd = 0.0
    return {
        "input_summary": raw_processing_log.get("input_summary")
        if isinstance(raw_processing_log.get("input_summary"), dict)
        else None,
        "model_used": raw_processing_log.get("model_used")
        if isinstance(raw_processing_log.get("model_used"), str)
        else None,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "cost_usd": cost_usd,
    }


def _policy_metadata(exc: BaseException) -> tuple[str, str, bool]:
    policy = classify_error(exc)
    if policy.error_class == "unknown" and exc.__cause__ is not None:
        policy = classify_error(exc.__cause__)
    return policy.status, policy.error_class, policy.retryable


def _run_processor_impl(
    processor_id: str,
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
    budget_context: BudgetContext | None = None,
    force: bool = False,
) -> dict | None:
    """Execute one processor and publish its result with explicit failure policy."""
    config = _resolved_config(config)
    correlation_id = correlation_id or str(uuid4())
    facade = _facade()
    if manage_lifecycle:
        facade.accept_run(config, correlation_id, "internal", "processor", processor_id)
        if not facade.start_run(config, correlation_id, f"sync:{uuid4()}"):
            return None

    import structlog.contextvars

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    processor = _dependency("get_processor", None)(processor_id)
    started_at = datetime.now(UTC)
    start_ms = time.monotonic() * 1000
    logger.info(
        "processor_started",
        action="run_processor",
        processor=processor_id,
        correlation_id=correlation_id,
    )

    status = "success"
    error_message = None
    error_class = None
    retryable = False
    result_payload = None
    failure_input_summary = None
    input_fingerprint = None
    reusable_output = None
    processor_processing_log = {}
    durable_output_id = None
    durable_output_ids: list[str] = []
    processing_log_persisted = False

    try:
        fingerprint_builder = _dependency(
            "build_processor_fingerprint",
            build_processor_fingerprint,
        )
        input_fingerprint = fingerprint_builder(processor, config)
    except Exception as exc:
        logger.warning(
            "processor_fingerprint_build_failed",
            action="run_processor",
            processor=processor_id,
            error=str(exc),
            correlation_id=correlation_id,
        )

    if input_fingerprint and not force:
        try:
            reusable_output = _dependency(
                "_find_reusable_processor_output", _find_reusable_processor_output
            )(processor_id, input_fingerprint, config)
        except Exception as exc:
            logger.warning(
                "processor_fingerprint_lookup_failed",
                action="run_processor",
                processor=processor_id,
                fingerprint_prefix=input_fingerprint[:12],
                error=str(exc),
                correlation_id=correlation_id,
            )
        if reusable_output is not None:
            status = "skipped"

    try:
        if status != "skipped":
            result_payload = processor.process(
                config, correlation_id, budget_context=budget_context
            )
            processor_processing_log = _normalize_processing_log(
                result_payload.get("processing_log")
                if isinstance(result_payload, dict)
                else None
            )
    except BudgetBlock as exc:
        status, error_class, retryable = _policy_metadata(exc)
        error_message = exc.safe_reason
        telemetry = getattr(exc, "telemetry", None)
        failure_input_summary = {
            "blocked_code": exc.code,
            **(
                telemetry.as_dict()
                if telemetry is not None and hasattr(telemetry, "as_dict")
                else {}
            ),
        }
        logger.warning(
            "processor_budget_blocked",
            action="run_processor",
            processor=processor_id,
            blocked_code=exc.code,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        status, error_class, retryable = _policy_metadata(exc)
        # Persisted/logged fields never carry raw exception text: provider or
        # model failures may echo opaque payloads, secrets, or prompt content
        # that named-value redaction cannot catch. The operator-facing contract
        # is carried by error_class; structured telemetry is bounded and safe.
        # LLMStageFailure is the sole exception to the generic template: its
        # message is a bounded constant chosen at the raise site (never
        # provider/model payload), so it is safe to surface verbatim — e.g.
        # "LLM response validation failed after retry".
        error_message = (
            str(exc)
            if isinstance(exc, LLMStageFailure)
            else f"processor failed ({error_class})"
        )
        telemetry = getattr(exc, "telemetry", None)
        if telemetry is not None and hasattr(telemetry, "as_dict"):
            failure_input_summary = telemetry.as_dict()
        logger.error(
            "processor_failed",
            action="run_processor",
            processor=processor_id,
            error_type=type(exc).__name__,
            error_class=error_class,
            correlation_id=correlation_id,
        )

    completed_at = datetime.now(UTC)
    duration_ms = int(time.monotonic() * 1000 - start_ms)

    if status == "success" and result_payload:
        opinions = (
            result_payload.get("opinions") if isinstance(result_payload, dict) else None
        )
        if isinstance(opinions, list):
            try:
                if not opinions or not all(
                    isinstance(opinion, dict) for opinion in opinions
                ):
                    raise InvalidSourceData(
                        "multi-opinion result must contain opinion objects"
                    )
                versioned_opinions = [
                    {
                        **opinion,
                        "correlation_id": correlation_id,
                        "lifecycle_status": "validated",
                        "schema_version": str(opinion.get("schema_version") or "1"),
                    }
                    for opinion in opinions
                ]
                if any(
                    not isinstance(opinion.get("opinion_id"), str)
                    or not opinion["opinion_id"]
                    for opinion in versioned_opinions
                ):
                    raise InvalidSourceData("processor opinion_id is required")
                durable_output_ids = [
                    opinion["opinion_id"] for opinion in versioned_opinions
                ]
                durable_output_id = durable_output_ids[0]
                atomic_processing_log = {
                    **processor_processing_log,
                    "output_id": durable_output_id,
                    "output_ids": durable_output_ids,
                    "prompt_text": None,
                    "raw_response": None,
                }
                _dependency("_persist_processor_result", None)(
                    opinions=versioned_opinions,
                    extra_records=result_payload.get("extra_records", {}),
                    processing_log=atomic_processing_log,
                    processor_id=processor_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                    correlation_id=correlation_id,
                    config=config,
                )
                processing_log_persisted = True
            except Exception as exc:
                status, error_class, retryable = _policy_metadata(
                    exc
                    if isinstance(exc, (InvalidSourceData, PersistenceError))
                    else PersistenceError("structured output DB write failed")
                )
                durable_output_id = None
                durable_output_ids = []
                # Bounded, raw-free: DB drivers may echo connection detail that
                # redaction cannot catch; error_class carries the category.
                error_message = (
                    str(exc)
                    if isinstance(exc, InvalidSourceData)
                    else "structured output DB write failed"
                )
                logger.error(
                    "processor_db_write_failed",
                    action="run_processor",
                    processor=processor_id,
                    error_type=type(exc).__name__,
                    error_class=error_class,
                    correlation_id=correlation_id,
                )

    if status == "success" and result_payload and not processing_log_persisted:
        any_output_written = False
        write_outcomes = []
        try:
            insert_records = _dependency("insert_records", None)
            upsert_records = _dependency("upsert_records", None)
            opinion = result_payload.get("opinion")
            if (
                not isinstance(opinion, dict)
                or not isinstance(opinion.get("opinion_id"), str)
                or not opinion["opinion_id"]
            ):
                raise InvalidSourceData(
                    "processor result must contain an opinion object"
                )
            opinion_written = insert_records(
                table_name="structured_opinions", records=[opinion], config=config
            )
            opinion_status = getattr(opinion_written, "status", "success")
            if opinion_status not in {"success", "partial", "failed"}:
                opinion_status = "success"
            write_outcomes.append(opinion_status)
            opinion_count = getattr(opinion_written, "written", None)
            opinion_durable = (
                opinion_count > 0
                if isinstance(opinion_count, int)
                and not isinstance(opinion_count, bool)
                else opinion_status != "failed"
            )
            if opinion_durable:
                durable_output_id = opinion.get("opinion_id")
                any_output_written = True
            extra_written = {}
            for table_name, records in result_payload.get("extra_records", {}).items():
                count = (
                    upsert_records(
                        table_name=table_name,
                        records=records,
                        conflict_columns=["briefing_date", "correlation_id"],
                        config=config,
                    )
                    if table_name == "daily_briefings"
                    else insert_records(
                        table_name=table_name, records=records, config=config
                    )
                )
                extra_written[table_name] = count
                extra_status = getattr(count, "status", "success")
                if extra_status not in {"success", "partial", "failed"}:
                    extra_status = "success"
                write_outcomes.append(extra_status)
                written_count = getattr(count, "written", None)
                if (
                    isinstance(written_count, int)
                    and not isinstance(written_count, bool)
                    and written_count > 0
                ):
                    any_output_written = True
            if any(outcome != "success" for outcome in write_outcomes):
                status = "partial" if any_output_written else "failed"
                error_message = "Structured output DB write was not fully durable"
                error_class, retryable = "persistence", True
            logger.info(
                "processor_records_written",
                action="run_processor",
                processor=processor_id,
                opinion_written=opinion_written,
                extra_written=extra_written,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            policy_error = (
                exc
                if isinstance(exc, (InvalidSourceData, PersistenceError))
                else PersistenceError("structured output DB write failed")
            )
            policy_status, error_class, retryable = _policy_metadata(policy_error)
            status = (
                policy_status
                if isinstance(exc, InvalidSourceData)
                else ("partial" if any_output_written else "failed")
            )
            error_message = (
                str(exc)
                if isinstance(exc, InvalidSourceData)
                else "structured output DB write failed"
            )
            logger.error(
                "processor_db_write_failed",
                action="run_processor",
                processor=processor_id,
                error_type=type(exc).__name__,
                error_class=error_class,
                correlation_id=correlation_id,
            )

    processing_log = dict(processor_processing_log)
    if status == "skipped":
        processing_log.update(
            {
                "status": "skipped",
                "tokens_input": 0,
                "tokens_output": 0,
                "cost_usd": 0.0,
            }
        )
    if failure_input_summary is not None:
        processing_log["input_summary"] = failure_input_summary
        processing_log["tokens_input"] = failure_input_summary.get(
            "tokens_input_total", 0
        )
        processing_log["tokens_output"] = failure_input_summary.get(
            "tokens_output_total", 0
        )
        processing_log["cost_usd"] = failure_input_summary.get("cost_usd_total", 0.0)
    processing_log.setdefault("tokens_input", 0)
    processing_log.setdefault("tokens_output", 0)
    processing_log.setdefault("cost_usd", 0.0)
    processing_log["prompt_text"] = None
    processing_log["raw_response"] = None
    processing_log.setdefault("processor", processor_id)
    processing_log["status"] = status
    processing_log["output_id"] = durable_output_id
    processing_log.setdefault("started_at", started_at)
    processing_log.setdefault("completed_at", completed_at)
    processing_log.setdefault("duration_ms", duration_ms)
    if error_message:
        processing_log["error_message"] = error_message

    if not processing_log_persisted:
        try:
            _dependency("_write_processing_log", None)(
                processor_id=processor_id,
                started_at=started_at,
                completed_at=completed_at,
                status=processing_log.get("status", status),
                input_summary=processing_log.get("input_summary"),
                output_id=processing_log.get("output_id"),
                prompt_text=processing_log.get("prompt_text"),
                raw_response=processing_log.get("raw_response"),
                model_used=processing_log.get("model_used"),
                tokens_input=processing_log.get("tokens_input"),
                tokens_output=processing_log.get("tokens_output"),
                cost_usd=processing_log.get("cost_usd"),
                duration_ms=duration_ms,
                error_message=error_message,
                input_fingerprint=input_fingerprint,
                skip_reason="unchanged_inputs" if status == "skipped" else None,
                forced=force,
                config=config,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            status = "failed"
            error_message = f"Processing log write failed: {exc}"
            error_class, retryable = "persistence", True

    result = {
        "processor": processor_id,
        "status": status,
        "duration_ms": duration_ms,
        "error": error_message,
        "error_class": error_class,
        "retryable": retryable,
        "correlation_id": correlation_id,
        "opinion_id": durable_output_id,
        "skip_reason": "unchanged_inputs" if status == "skipped" else None,
        "reusable_output": status == "skipped" and reusable_output is not None,
        "forced": force,
    }
    if durable_output_ids:
        result["opinion_ids"] = durable_output_ids
    if input_fingerprint:
        result["input_fingerprint"] = input_fingerprint
    logger.info(
        "processor_work_finished",
        action="run_processor",
        **{
            **result,
            "input_fingerprint": input_fingerprint[:12] if input_fingerprint else None,
        },
    )
    if manage_lifecycle:
        facade.finalize_run_safely(
            correlation_id,
            status,
            result,
            config,
            error_message,
            run_kind="processor",
            component=processor_id,
        )
    return result


def run_processor(
    processor_id: str,
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
    budget_context: BudgetContext | None = None,
    force: bool = False,
) -> dict | None:
    config = _resolved_config(config)
    correlation_id = correlation_id or str(uuid4())
    facade = _facade()
    lifecycle_created = False
    worker_id: str | None = None
    try:
        if manage_lifecycle:
            facade.accept_run(
                config, correlation_id, "internal", "processor", processor_id
            )
            lifecycle_created = True
            claim_worker_id = f"sync:{uuid4()}"
            if not facade.start_run(config, correlation_id, claim_worker_id):
                return None
            worker_id = claim_worker_id
            if budget_context is None:
                authorize = _dependency(
                    "_authorize_claimed_run_budget", _authorize_claimed_run_budget
                )
                budget_context = authorize(
                    config,
                    correlation_id,
                    run_kind="processor",
                    component=processor_id,
                )
        heartbeat = (
            facade.maintain_run_heartbeat(config, correlation_id, worker_id)
            if worker_id is not None
            else __import__("contextlib").nullcontext()
        )
        with heartbeat:
            with facade.advisory_lock(f"processor:{processor_id}", config):
                impl = getattr(facade, "_run_processor_impl", _run_processor_impl)
                result = impl(
                    processor_id,
                    config,
                    correlation_id,
                    manage_lifecycle=False,
                    budget_context=budget_context,
                    force=force,
                )
            if manage_lifecycle and result is not None:
                finalized = facade.finalize_run_safely(
                    correlation_id,
                    result["status"],
                    result,
                    config,
                    result.get("error"),
                    worker_id=worker_id,
                    run_kind="processor",
                    component=processor_id,
                )
                if finalized:
                    logger.info("processor_completed", action="run_processor", **result)
            return result
    except facade.RunAcceptanceConflict:
        raise
    except Exception as exc:
        if manage_lifecycle and lifecycle_created:
            policy = facade.classify_error(exc)
            error_message = (
                str(exc) if worker_id is not None else "run start unavailable"
            )
            result = (
                {}
                if worker_id is not None
                else {
                    "status": policy.status,
                    "reason": error_message,
                    "error_class": policy.error_class,
                    "retryable": policy.retryable,
                }
            )
            facade.finalize_run_safely(
                correlation_id,
                "failed",
                result,
                config,
                error_message,
                worker_id=worker_id,
                run_kind="processor",
                component=processor_id,
            )
        raise


def _consume_budget_override(
    correlation_id: str,
    config: dict,
    *,
    run_kind: str | None = None,
    component: str | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Atomically consume a validated one-run override for this worker claim.

    Returns the override only when every integrity check passes: the run row
    exists for this correlation, the override is a requested one-run record,
    it has a requestor, it has not expired, its run kind and requested
    component match the run being claimed, and it was not already consumed by
    another run. Anything missing or inconsistent returns ``None`` so callers
    fail closed into normal budget enforcement.
    """
    current = now or datetime.now(UTC)
    with _get_session(config) as session:
        row = session.execute(
            text(
                "SELECT summary, run_kind, requested_component FROM cycle_runs "
                "WHERE correlation_id = :cid FOR UPDATE"
            ),
            {"cid": correlation_id},
        ).fetchone()
        if not row:
            return None
        mapping = row._mapping
        summary = mapping.get("summary") or {}
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except (TypeError, ValueError):
                logger.warning(
                    "budget_override_summary_invalid",
                    correlation_id=correlation_id,
                )
                return None
        override = (
            summary.get("budget_override") if isinstance(summary, dict) else None
        )
        if not isinstance(override, dict) or override.get("requested") is not True:
            return None
        if override.get("consumed_at"):
            # One-time: only the consuming run may re-read its own override.
            return override if override.get("consumed_by") == correlation_id else None
        requested_by = override.get("requested_by")
        if not isinstance(requested_by, str) or not requested_by.strip():
            logger.warning(
                "budget_override_missing_requestor", correlation_id=correlation_id
            )
            return None
        expires_raw = override.get("expires_at")
        try:
            expires_at = datetime.fromisoformat(str(expires_raw))
        except (TypeError, ValueError):
            logger.warning(
                "budget_override_invalid_expiry", correlation_id=correlation_id
            )
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= current:
            logger.warning(
                "budget_override_expired", correlation_id=correlation_id
            )
            return None
        effective_run_kind = (
            run_kind if run_kind is not None else mapping.get("run_kind")
        )
        effective_component = (
            component
            if component is not None
            else mapping.get("requested_component")
        )
        if override.get("run_kind") != effective_run_kind:
            logger.warning(
                "budget_override_run_kind_mismatch",
                correlation_id=correlation_id,
                override_run_kind=override.get("run_kind"),
                run_kind=effective_run_kind,
            )
            return None
        if override.get("requested_component") != effective_component:
            logger.warning(
                "budget_override_component_mismatch",
                correlation_id=correlation_id,
                override_component=override.get("requested_component"),
                component=effective_component,
            )
            return None
        override = {
            **override,
            "consumed_at": current.isoformat(),
            "consumed_by": correlation_id,
        }
        summary["budget_override"] = override
        session.execute(
            text(
                "UPDATE cycle_runs SET summary = CAST(:summary AS JSONB) "
                "WHERE correlation_id = :cid"
            ),
            {"cid": correlation_id, "summary": json.dumps(summary)},
        )
    logger.warning(
        "budget_override_consumed",
        correlation_id=correlation_id,
        reason=override.get("reason"),
        requested_by=override.get("requested_by"),
    )
    return override


def _authorize_claimed_run_budget(
    config: dict,
    correlation_id: str,
    *,
    run_kind: str,
    component: str | None,
) -> BudgetContext | None:
    """Consume a validated override at worker claim and mint the trusted context.

    The trusted authorization is minted here — inside the worker/orchestrator
    execution path, after the run was durably claimed — never at an HTTP
    acceptance boundary. Any consumption failure (including unreadable budget
    state) fails closed: no context is minted and normal budget enforcement
    applies.
    """
    from budgets import (
        mint_trusted_manual_authorization,
        trusted_manual_budget_context,
    )

    try:
        override = _consume_budget_override(
            correlation_id, config, run_kind=run_kind, component=component
        )
    except Exception as exc:
        logger.warning(
            "budget_override_claim_failed",
            correlation_id=correlation_id,
            run_kind=run_kind,
            component=component,
            error_type=type(exc).__name__,
        )
        return None
    if override is None:
        return None
    return trusted_manual_budget_context(
        force=True,
        manual_authorized=True,
        authorization=mint_trusted_manual_authorization(),
    )


# Publication owns the durable implementations. These aliases are intentionally
# module-level so the facade can re-export the historical patch targets.
from publication import _persist_processor_result, _write_processing_log  # noqa: E402

__all__ = [
    "build_processor_fingerprint",
    "_find_reusable_processor_output",
    "_resolve_and_run_processors",
    "_run_processor_impl",
    "run_processor",
    "_consume_budget_override",
    "_authorize_claimed_run_budget",
    "_persist_processor_result",
    "_write_processing_log",
]
