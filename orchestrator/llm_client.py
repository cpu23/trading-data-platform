import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from budgets import BudgetBlock, BudgetContext, BudgetPermit, enforce_budget
from http_client import make_request
from logging_config import get_logger

logger = get_logger("llm_client")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Single pinned production model slug (spec §2.1). No floating "latest" alias.
DEFAULT_MODEL_SLUG = "deepseek/deepseek-v4-flash-0731"
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
    tokens_reasoning_total: int = 0
    tokens_cached_total: int = 0
    cost_usd_total: float = 0.0
    last_requested_model: str = ""
    last_resolved_model: str = ""
    last_provider: str | None = None
    last_retry_count: int = 0
    last_generation_id: str | None = None

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
            "tokens_reasoning_total": self.tokens_reasoning_total,
            "tokens_cached_total": self.tokens_cached_total,
            "cost_usd_total": self.cost_usd_total,
            "requested_model": self.last_requested_model,
            "resolved_model": self.last_resolved_model,
            "provider": self.last_provider,
            "retry_count": self.last_retry_count,
            "generation_id": self.last_generation_id,
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


_warned_legacy_model_keys: set[str] = set()


def _models_map(llm_config: dict) -> dict:
    models = llm_config.get("models", {})
    return models if isinstance(models, dict) else {}


def _legacy_model_keys(llm_config: dict) -> list[str]:
    """List deprecated model selector keys present in the configuration."""
    keys: list[str] = []
    legacy_default = llm_config.get("default_model")
    if isinstance(legacy_default, str) and legacy_default.strip():
        keys.append("llm.default_model")
    for processor, value in _models_map(llm_config).items():
        if processor == "default":
            continue
        override = value.get("model") if isinstance(value, dict) else value
        if isinstance(override, str) and override.strip():
            keys.append(f"llm.models.{processor}")
    return keys


def _warn_legacy_model_config(llm_config: dict) -> None:
    keys = _legacy_model_keys(llm_config)
    unseen = [key for key in keys if key not in _warned_legacy_model_keys]
    if not unseen:
        return
    _warned_legacy_model_keys.update(unseen)
    logger.warning(
        "llm_model_config_deprecated",
        legacy_keys=unseen,
        replacement="llm.models.default",
    )


def resolve_model(
    config: dict, processor_id: str | None = None, model: str | None = None
) -> str:
    """Resolve the single active model slug for every LLM processor.

    Precedence: explicit call argument, then ``llm.models.default`` (the one
    source of truth), then deprecated legacy selectors for one release.
    """
    if model:
        return model
    llm_config = config.get("llm", {})
    default = _models_map(llm_config).get("default")
    if isinstance(default, str) and default.strip():
        return default.strip()
    _warn_legacy_model_config(llm_config)
    if processor_id:
        override = _models_map(llm_config).get(processor_id)
        if isinstance(override, dict):
            override = override.get("model")
        if isinstance(override, str) and override.strip():
            return override.strip()
    legacy_default = llm_config.get("default_model")
    if isinstance(legacy_default, str) and legacy_default.strip():
        return legacy_default.strip()
    return DEFAULT_MODEL_SLUG


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
    if (
        not isinstance(effective_temperature, (int, float))
        or not 0 <= effective_temperature <= 2
    ):
        raise ValueError(f"llm temperature for {processor_id} must be between 0 and 2")

    stage_timeout = timeout
    if stage_timeout is None:
        stage_timeout = llm_config.get(
            "stage_timeout_seconds", DEFAULT_STAGE_TIMEOUT_SECONDS
        )
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
    if (
        not isinstance(request_attempts, int)
        or isinstance(request_attempts, bool)
        or request_attempts < 1
    ):
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
        response_schema: dict | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.processor_id = processor_id
        self.correlation_id = correlation_id
        self.budget_context = budget_context or BudgetContext()
        self.response_schema = response_schema
        self.reasoning_effort = reasoning_effort
        self.clock = clock
        self.policy = resolve_request_policy(
            config,
            processor_id,
            model=model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
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
        self.telemetry.tokens_input_total += _safe_token_count(
            result.get("tokens_input")
        )
        self.telemetry.tokens_output_total += _safe_token_count(
            result.get("tokens_output")
        )
        self.telemetry.tokens_reasoning_total += _safe_token_count(
            result.get("tokens_reasoning")
        )
        self.telemetry.tokens_cached_total += _safe_token_count(
            result.get("tokens_cached")
        )
        self.telemetry.cost_usd_total += _safe_cost_usd(result.get("cost_usd"))
        self.telemetry.last_requested_model = str(
            result.get("requested_model") or self.telemetry.last_requested_model
        )
        self.telemetry.last_resolved_model = str(
            result.get("model") or self.telemetry.last_resolved_model
        )
        self.telemetry.last_provider = result.get("provider")
        self.telemetry.last_retry_count = _safe_token_count(result.get("retry_count"))
        self.telemetry.last_generation_id = result.get("generation_id")

    def call(self, prompt: str) -> dict:
        if self.telemetry.attempt_count >= 1 + self.policy.validation_retries:
            raise LLMStageFailure(
                "LLM validation retry limit exhausted", self.telemetry
            )

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
                response_schema=self.response_schema,
                reasoning_effort=self.reasoning_effort,
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
    messages: list[dict] | None = None,
    processor_id: str = "default",
    max_output_tokens: int | None = None,
    timeout: float | None = None,
    structured_response: bool | None = None,
    response_schema: dict | None = None,
    reasoning_effort: str | None = None,
    include_temperature: bool | None = None,
    budget_context: BudgetContext | None = None,
    _budget_permit: BudgetPermit | None = None,
) -> dict:
    if config is None:
        from config_loader import load_config

        config = load_config()

    llm_config = config["llm"]
    if include_temperature is None:
        include_temperature = llm_config.get("include_temperature", True)
    if not isinstance(include_temperature, bool):
        raise ValueError("llm.include_temperature must be a boolean")
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

    if messages is None:
        request_messages = [{"role": "user", "content": prompt}]
    else:
        if not messages:
            raise ValueError("messages must be a non-empty array")
        request_messages = []
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("each message must be an object")
            role = message.get("role")
            content = message.get("content")
            if role not in {"system", "user", "assistant"}:
                raise ValueError("message role is invalid")
            if not isinstance(content, str) or not content:
                raise ValueError("message content must be a non-empty string")
            request_messages.append(dict(message))
    request_body = {
        "model": policy.model,
        "messages": request_messages,
        "max_tokens": policy.max_output_tokens,
    }
    if include_temperature:
        request_body["temperature"] = policy.temperature
    provider_preferences = {}
    max_price = _processor_value(llm_config, "max_prices", processor_id, None)
    if max_price is not None:
        if not isinstance(max_price, dict) or not max_price:
            raise ValueError(
                f"llm.max_prices for {processor_id} must be a non-empty object"
            )
        provider_preferences["max_price"] = dict(max_price)

    if response_schema is not None:
        if not isinstance(response_schema, dict):
            raise ValueError("response_schema must be a JSON Schema object")
        request_body["response_format"] = {
            "type": "json_schema",
            "json_schema": response_schema,
        }
        if _processor_value(
            llm_config,
            "require_parameters",
            processor_id,
            True,
        ):
            provider_preferences["require_parameters"] = True
    elif policy.structured_response:
        request_body["response_format"] = {"type": "json_object"}
        if _processor_value(
            llm_config,
            "require_parameters",
            processor_id,
            True,
        ):
            provider_preferences["require_parameters"] = True
    if provider_preferences:
        request_body["provider"] = provider_preferences
    if reasoning_effort is not None:
        request_body["reasoning"] = {"effort": reasoning_effort}

    api_key = _processor_value(
        llm_config,
        "api_keys",
        processor_id,
        llm_config["api_key"],
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
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
    details = usage.get("details")
    prompt_details = usage.get("prompt_tokens_details")
    tokens_input = _safe_token_count(usage.get("prompt_tokens", 0))
    tokens_output = _safe_token_count(usage.get("completion_tokens", 0))
    tokens_reasoning = _safe_token_count(
        (details or {}).get("reasoning_tokens", 0) if isinstance(details, dict) else 0
    )
    tokens_cached = _safe_token_count(
        (prompt_details or {}).get("cached_tokens", 0)
        if isinstance(prompt_details, dict)
        else 0
    )
    model_used = data.get("model", policy.model)
    provider_name = data.get("provider")
    cost_usd = _safe_cost_usd(usage.get("cost"))
    generation_id = data.get("id")
    transport_metadata = getattr(response, "extensions", {}).get("request_metadata")
    if not isinstance(transport_metadata, dict):
        transport_metadata = {}
    retry_count = max(0, _safe_token_count(transport_metadata.get("attempts")) - 1)
    schema_valid_first_pass = None if response_schema is None else True

    logger.info(
        "llm_call_completed",
        action="llm_call",
        model=model_used,
        requested_model=policy.model,
        provider=provider_name or "unknown",
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_reasoning=tokens_reasoning,
        tokens_cached=tokens_cached,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        retry_count=retry_count,
        correlation_id=correlation_id or "none",
    )
    return {
        "content": content,
        "model": model_used,
        "requested_model": policy.model,
        "provider": provider_name if isinstance(provider_name, str) else None,
        "generation_id": generation_id if isinstance(generation_id, str) else None,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "tokens_reasoning": tokens_reasoning,
        "tokens_cached": tokens_cached,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "retry_count": retry_count,
        "schema_valid_first_pass": schema_valid_first_pass,
    }


def model_preflight(config: dict, model: str | None = None) -> dict:
    """Verify the active model slug without performing paid inference.

    Resolves the slug exactly as processors do, then checks it against the
    OpenRouter public model catalogue. Read-only and credential-light: the
    catalogue endpoint does not require authentication.
    """
    slug = resolve_model(config, model=model)
    llm_config = config.get("llm", {})
    api_key = llm_config.get("api_key") or ""
    headers = {"HTTP-Referer": "https://github.com/trading-data-platform"}
    if isinstance(api_key, str) and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = make_request(
            method="GET",
            url="https://openrouter.ai/api/v1/models",
            headers=headers,
            timeout=15.0,
            max_retries=1,
        )
        response.raise_for_status()
        catalogue = response.json().get("data")
    except Exception as exc:
        return {
            "model": slug,
            "listed": None,
            "error": f"model catalogue unreachable ({type(exc).__name__})",
        }
    if not isinstance(catalogue, list):
        return {
            "model": slug,
            "listed": None,
            "error": "model catalogue returned an unexpected payload",
        }
    matched = next(
        (
            entry
            for entry in catalogue
            if isinstance(entry, dict) and entry.get("id") == slug
        ),
        None,
    )
    if matched is None:
        return {
            "model": slug,
            "listed": False,
            "error": "slug not present in the OpenRouter catalogue",
        }
    return {
        "model": slug,
        "listed": True,
        "structured_outputs": bool(
            isinstance(matched.get("supported_parameters"), list)
            and "structured_outputs" in matched["supported_parameters"]
        ),
        "context_length": matched.get("context_length"),
    }
