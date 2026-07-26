import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from budgets import BudgetBlock, BudgetContext, BudgetPermit, enforce_budget
from http_client import make_request
from logging_config import get_logger

logger = get_logger("llm_client")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MAX_OUTPUT_TOKENS = 2048
MAX_ALLOWED_OUTPUT_TOKENS = 4096
DEFAULT_STAGE_TIMEOUT_SECONDS = 90.0
DEFAULT_VALIDATION_RETRIES = 1
MAX_VALIDATION_RETRIES = 1
MAX_SAFE_WARNINGS = 10
MAX_SAFE_WARNING_LENGTH = 200


@dataclass(frozen=True)
class LLMRequestPolicy:
    model: str
    max_output_tokens: int
    temperature: float
    stage_timeout_seconds: float
    validation_retries: int
    request_attempts: int
    structured_response: bool


@dataclass
class LLMAttemptTelemetry:
    attempt_count: int = 0
    first_attempt_duration_ms: int | None = None
    validation_retry_duration_ms: int | None = None
    validation_warnings: list[str] = field(default_factory=list)
    model: str = ""
    max_output_tokens: int = 0
    tokens_input_total: int = 0
    tokens_output_total: int = 0
    cost_usd_total: float = 0.0

    def as_dict(self) -> dict:
        return {
            "attempt_count": self.attempt_count,
            "first_attempt_duration_ms": self.first_attempt_duration_ms,
            "validation_retry_duration_ms": self.validation_retry_duration_ms,
            "validation_warnings": list(self.validation_warnings),
            "model": self.model,
            "max_output_tokens": self.max_output_tokens,
            "tokens_input_total": self.tokens_input_total,
            "tokens_output_total": self.tokens_output_total,
            "cost_usd_total": self.cost_usd_total,
        }


class LLMStageFailure(RuntimeError):
    def __init__(self, message: str, telemetry: LLMAttemptTelemetry):
        super().__init__(message)
        self.telemetry = telemetry


class LLMStageTimeout(LLMStageFailure):
    pass


class LLMValidationError(LLMStageFailure):
    code = "llm_validation_failed"


def _safe_token_count(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed >= 0 else 0


def _safe_cost_usd(value) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0


def _processor_value(llm_config: dict, key: str, processor_id: str, default):
    configured = llm_config.get(key, default)
    if isinstance(configured, dict):
        return configured.get(processor_id, default)
    return configured


def resolve_model(config: dict, processor_id: str | None = None, model: str | None = None) -> str:
    """Resolve a provider/model identifier without coupling processors to a provider."""
    if model:
        return model
    llm_config = config.get("llm", {})
    if processor_id:
        override = llm_config.get("models", {}).get(processor_id)
        if override:
            return override
    return llm_config.get("default_model", "deepseek/deepseek-v4-flash")


def resolve_request_policy(
    config: dict,
    processor_id: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    timeout: float | None = None,
    structured_response: bool | None = None,
) -> LLMRequestPolicy:
    llm_config = config.get("llm", {})
    effective_max_tokens = max_output_tokens
    if effective_max_tokens is None:
        effective_max_tokens = _processor_value(
            llm_config, "max_output_tokens", processor_id, DEFAULT_MAX_OUTPUT_TOKENS
        )
    if (
        not isinstance(effective_max_tokens, int)
        or isinstance(effective_max_tokens, bool)
        or effective_max_tokens < 1
        or effective_max_tokens > MAX_ALLOWED_OUTPUT_TOKENS
    ):
        raise ValueError(
            f"llm.max_output_tokens for {processor_id} must be between 1 and "
            f"{MAX_ALLOWED_OUTPUT_TOKENS}"
        )

    effective_temperature = temperature
    if effective_temperature is None:
        effective_temperature = _processor_value(
            llm_config,
            "temperatures",
            processor_id,
            llm_config.get("temperature", 0.2),
        )
    if not isinstance(effective_temperature, (int, float)) or not 0 <= effective_temperature <= 2:
        raise ValueError(f"llm temperature for {processor_id} must be between 0 and 2")

    stage_timeout = timeout
    if stage_timeout is None:
        stage_timeout = llm_config.get("stage_timeout_seconds", DEFAULT_STAGE_TIMEOUT_SECONDS)
    if not isinstance(stage_timeout, (int, float)) or stage_timeout <= 0:
        raise ValueError("llm.stage_timeout_seconds must be positive")

    validation_retries = llm_config.get(
        "validation_retries", DEFAULT_VALIDATION_RETRIES
    )
    if (
        not isinstance(validation_retries, int)
        or isinstance(validation_retries, bool)
        or not 0 <= validation_retries <= MAX_VALIDATION_RETRIES
    ):
        raise ValueError(
            f"llm.validation_retries must be between 0 and {MAX_VALIDATION_RETRIES}"
        )

    request_attempts = llm_config.get("max_retries", 1)
    if not isinstance(request_attempts, int) or isinstance(request_attempts, bool) or request_attempts < 1:
        raise ValueError("llm.max_retries must be at least 1")

    if structured_response is None:
        structured_response = bool(
            _processor_value(llm_config, "structured_response", processor_id, False)
        )

    return LLMRequestPolicy(
        model=resolve_model(config, processor_id=processor_id, model=model),
        max_output_tokens=effective_max_tokens,
        temperature=float(effective_temperature),
        stage_timeout_seconds=float(stage_timeout),
        validation_retries=validation_retries,
        request_attempts=request_attempts,
        structured_response=bool(structured_response),
    )


class LLMStage:
    """Own one processor's non-resetting LLM deadline and safe attempt telemetry."""

    def __init__(
        self,
        config: dict,
        processor_id: str,
        *,
        correlation_id: str | None = None,
        budget_context: BudgetContext | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.processor_id = processor_id
        self.correlation_id = correlation_id
        self.budget_context = budget_context or BudgetContext()
        self.clock = clock
        self.policy = resolve_request_policy(config, processor_id)
        self._deadline = clock() + self.policy.stage_timeout_seconds
        self.telemetry = LLMAttemptTelemetry(
            model=self.policy.model,
            max_output_tokens=self.policy.max_output_tokens,
        )
        self._budget_permit: BudgetPermit | None = None

    def add_validation_warnings(self, warnings: list[str]) -> None:
        remaining = MAX_SAFE_WARNINGS - len(self.telemetry.validation_warnings)
        if remaining <= 0:
            return
        self.telemetry.validation_warnings.extend(
            str(warning).replace("\n", " ")[:MAX_SAFE_WARNING_LENGTH]
            for warning in warnings[:remaining]
        )

    def _record_attempt(self, started_at: float) -> float:
        completed_at = self.clock()
        duration_ms = max(0, int((completed_at - started_at) * 1000))
        self.telemetry.attempt_count += 1
        if self.telemetry.attempt_count == 1:
            self.telemetry.first_attempt_duration_ms = duration_ms
        else:
            self.telemetry.validation_retry_duration_ms = duration_ms
        return completed_at

    def _record_usage(self, result: dict) -> None:
        self.telemetry.tokens_input_total += _safe_token_count(result.get("tokens_input"))
        self.telemetry.tokens_output_total += _safe_token_count(result.get("tokens_output"))
        self.telemetry.cost_usd_total += _safe_cost_usd(result.get("cost_usd"))

    def call(self, prompt: str) -> dict:
        if self.telemetry.attempt_count >= 1 + self.policy.validation_retries:
            raise LLMStageFailure("LLM validation retry limit exhausted", self.telemetry)

        remaining = self._deadline - self.clock()
        if remaining <= 0:
            raise LLMStageTimeout("LLM stage deadline exhausted", self.telemetry)

        if self._budget_permit is None:
            try:
                self._budget_permit = enforce_budget(
                    self.config, self.processor_id, self.budget_context
                )
            except BudgetBlock as exc:
                exc.telemetry = self.telemetry
                raise

        started_at = self.clock()
        try:
            result = call_llm(
                prompt=prompt,
                processor_id=self.processor_id,
                correlation_id=self.correlation_id,
                config=self.config,
                model=self.policy.model,
                temperature=self.policy.temperature,
                max_output_tokens=self.policy.max_output_tokens,
                timeout=remaining,
                structured_response=self.policy.structured_response,
                max_retries=self.policy.request_attempts,
                _budget_permit=self._budget_permit,
            )
        except Exception as exc:
            if isinstance(exc, LLMStageFailure):
                raise
            completed_at = self._record_attempt(started_at)
            if completed_at >= self._deadline:
                raise LLMStageTimeout(
                    "LLM stage deadline exhausted", self.telemetry
                ) from exc
            raise LLMStageFailure("LLM request failed", self.telemetry) from exc

        completed_at = self._record_attempt(started_at)
        self._record_usage(result)
        # The sync HTTP timeout is the primary interruption mechanism. This
        # post-call guard also rejects transports/mocks that return too late.
        if completed_at >= self._deadline:
            raise LLMStageTimeout("LLM stage deadline exhausted", self.telemetry)
        return result


def call_llm(
    prompt: str,
    model: str | None = None,
    temperature: float | None = None,
    max_retries: int | None = None,
    correlation_id: str | None = None,
    config: dict | None = None,
    *,
    processor_id: str = "default",
    max_output_tokens: int | None = None,
    timeout: float | None = None,
    structured_response: bool | None = None,
    reasoning_effort: str | None = None,
    budget_context: BudgetContext | None = None,
    _budget_permit: BudgetPermit | None = None,
) -> dict:
    if config is None:
        from config_loader import load_config
        config = load_config()

    llm_config = config["llm"]
    policy = resolve_request_policy(
        config,
        processor_id,
        model=model,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        timeout=timeout,
        structured_response=structured_response,
    )
    if _budget_permit is None or not _budget_permit.valid:
        _budget_permit = enforce_budget(config, processor_id, budget_context)
    request_attempts = policy.request_attempts if max_retries is None else max_retries

    request_body = {
        "model": policy.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": policy.temperature,
        "max_tokens": policy.max_output_tokens,
    }
    if policy.structured_response:
        request_body["response_format"] = {"type": "json_object"}
    if reasoning_effort is not None:
        request_body["reasoning"] = {"effort": reasoning_effort}

    headers = {
        "Authorization": f"Bearer {llm_config['api_key']}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/trading-data-platform",
    }
    started_at = time.monotonic()
    logger.info(
        "llm_call_started",
        action="llm_call",
        model=policy.model,
        max_output_tokens=policy.max_output_tokens,
        correlation_id=correlation_id or "none",
    )

    try:
        response = make_request(
            method="POST",
            url=OPENROUTER_URL,
            headers=headers,
            json_body=request_body,
            timeout=policy.stage_timeout_seconds,
            max_retries=request_attempts,
            correlation_id=correlation_id,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.error(
            "llm_call_failed",
            action="llm_call",
            model=policy.model,
            error_type=type(exc).__name__,
            duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            correlation_id=correlation_id or "none",
        )
        raise

    duration_ms = max(0, int((time.monotonic() - started_at) * 1000))
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    tokens_input = _safe_token_count(usage.get("prompt_tokens", 0))
    tokens_output = _safe_token_count(usage.get("completion_tokens", 0))
    model_used = data.get("model", policy.model)
    cost_usd = _safe_cost_usd(usage.get("cost"))

    logger.info(
        "llm_call_completed",
        action="llm_call",
        model=model_used,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        correlation_id=correlation_id or "none",
    )
    return {
        "content": content,
        "model": model_used,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
    }
