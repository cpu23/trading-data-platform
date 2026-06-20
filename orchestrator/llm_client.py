import time
from typing import Any

import httpx

from http_client import make_request
from logging_config import get_logger

logger = get_logger("llm_client")

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
_UNSUPPORTED_MARKERS = (
    "unsupported",
    "not supported",
    "does not support",
    "unknown parameter",
    "unrecognized parameter",
    "extra inputs are not permitted",
)


def call_llm(
    prompt: str,
    model: str | None = None,
    temperature: float | None = None,
    max_retries: int | None = None,
    correlation_id: str | None = None,
    config: dict | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    provider_preferences: dict | None = None,
) -> dict:
    if config is None:
        from config_loader import load_config

        config = load_config()

    llm_config = config["llm"]
    provider = str(llm_config.get("provider") or "openai-compatible")
    base_url = str(llm_config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    url = _chat_completions_url(base_url)
    api_key = llm_config.get("api_key")
    effective_model = resolve_model(config, model=model)
    effective_retries = (
        int(max_retries)
        if max_retries is not None
        else int(llm_config.get("max_retries", 3))
    )
    timeout = float(llm_config.get("timeout_seconds", 120.0))

    sampling = llm_config.get("sampling", {})
    if not isinstance(sampling, dict):
        sampling = {}
    configured_temperature = sampling.get(
        "temperature", llm_config.get("temperature", 0.2)
    )
    effective_temperature = (
        temperature if temperature is not None else configured_temperature
    )
    effective_top_p = sampling.get("top_p", llm_config.get("top_p"))

    configured_reasoning = llm_config.get("reasoning_effort")
    reasoning_fallback = None
    if isinstance(configured_reasoning, dict):
        reasoning_fallback = configured_reasoning.get("fallback")
        configured_reasoning = configured_reasoning.get(
            "effort", configured_reasoning.get("value")
        )
    effective_reasoning = (
        reasoning_effort if reasoning_effort is not None else configured_reasoning
    )

    request_body: dict[str, Any] = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if effective_temperature is not None:
        request_body["temperature"] = effective_temperature
    if effective_top_p is not None:
        request_body["top_p"] = effective_top_p
    if effective_reasoning is not None:
        request_body["reasoning_effort"] = effective_reasoning
    if max_tokens is not None:
        request_body["max_tokens"] = int(max_tokens)
    if provider_preferences:
        request_body["provider"] = provider_preferences

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if provider.lower() == "openrouter":
        headers["HTTP-Referer"] = llm_config.get(
            "http_referer", "https://github.com/trading-data-platform"
        )
    extra_headers = llm_config.get("headers")
    if isinstance(extra_headers, dict):
        headers.update({str(key): str(value) for key, value in extra_headers.items()})

    start = time.monotonic()
    total_attempts = 0
    transport_retries = 0
    capability_fallback_attempts = 0
    fallback_parameters: list[str] = []

    logger.info(
        "llm_call_started",
        action="llm_call",
        provider=provider,
        model=effective_model,
        prompt_length=len(prompt),
        correlation_id=correlation_id or "none",
    )

    try:
        while True:
            response = make_request(
                method="POST",
                url=url,
                headers=headers,
                json_body=request_body.copy(),
                timeout=timeout,
                max_retries=effective_retries,
                correlation_id=correlation_id,
            )
            total_attempts += _response_attempts(response)
            transport_retries += _response_retries(response)

            if response.status_code < 400:
                break

            unsupported_parameter = _explicit_unsupported_parameter(
                response, tuple(request_body)
            )
            if (
                unsupported_parameter == "reasoning_effort"
                and _auto_fallback_enabled(
                    llm_config, "reasoning_effort", reasoning_fallback
                )
            ):
                request_body.pop("reasoning_effort", None)
                fallback_parameters.append(unsupported_parameter)
                capability_fallback_attempts += 1
                continue

            if (
                unsupported_parameter in {"temperature", "top_p"}
                and _auto_fallback_enabled(llm_config, "sampling")
            ):
                removed = [
                    parameter
                    for parameter in ("temperature", "top_p")
                    if request_body.pop(parameter, None) is not None
                ]
                fallback_parameters.extend(removed)
                capability_fallback_attempts += 1
                continue

            response.raise_for_status()
    except Exception as exc:
        total_attempts += _exception_attempts(exc)
        transport_retries += _exception_retries(exc)
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "llm_call_failed",
            action="llm_call",
            provider=provider,
            model=effective_model,
            error=str(exc),
            attempts=total_attempts,
            duration_ms=duration_ms,
            correlation_id=correlation_id or "none",
            prompt=prompt,
        )
        raise

    duration_ms = int((time.monotonic() - start) * 1000)
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    tokens_input = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    tokens_output = int(
        usage.get("completion_tokens") or usage.get("output_tokens") or 0
    )
    total_tokens = int(usage.get("total_tokens") or tokens_input + tokens_output)
    model_used = data.get("model", effective_model)

    cost_usd = usage.get("cost")
    if cost_usd is not None:
        cost_usd = float(cost_usd)

    provider_request_id = _provider_request_id(response, data)
    request_metadata = {
        "provider": provider,
        "provider_returned": data.get("provider"),
        "base_url": base_url,
        "url": url,
        "model_requested": effective_model,
        "model_returned": model_used,
        "provider_request_id": provider_request_id,
        "attempts": total_attempts,
        "transport_retries": transport_retries,
        "capability_fallback_attempts": capability_fallback_attempts,
        "duration_ms": duration_ms,
        "fallback_parameters": fallback_parameters,
        "reasoning_effort_requested": effective_reasoning,
        "reasoning_effort_applied": request_body.get("reasoning_effort"),
        "sampling_requested": {
            key: value
            for key, value in {
                "temperature": effective_temperature,
                "top_p": effective_top_p,
            }.items()
            if value is not None
        },
        "sampling_applied": {
            key: request_body[key]
            for key in ("temperature", "top_p")
            if key in request_body
        },
        "max_tokens": request_body.get("max_tokens"),
        "provider_preferences": request_body.get("provider"),
    }
    result = {
        "content": content,
        "model": model_used,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "attempts": total_attempts,
        "provider": provider,
        "provider_request_id": provider_request_id,
        "usage": usage,
        "request_metadata": request_metadata,
    }

    logger.info(
        "llm_call_completed",
        action="llm_call",
        provider=provider,
        model=model_used,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        attempts=total_attempts,
        duration_ms=duration_ms,
        provider_request_id=provider_request_id,
        fallback_parameters=fallback_parameters,
        correlation_id=correlation_id or "none",
        response=content,
    )

    return result


def resolve_model(
    config: dict, processor_id: str | None = None, model: str | None = None
) -> str:
    """Resolve a model without coupling processors to a provider."""
    if model:
        return model
    llm_config = config.get("llm", {})
    if processor_id:
        overrides = {}
        if isinstance(llm_config.get("models"), dict):
            overrides.update(llm_config["models"])
        if isinstance(llm_config.get("model_overrides"), dict):
            overrides.update(llm_config["model_overrides"])
        override = overrides.get(processor_id) if isinstance(overrides, dict) else None
        if isinstance(override, dict):
            override = override.get("model")
        if override:
            return str(override)
    return str(llm_config.get("default_model") or DEFAULT_MODEL)


def _chat_completions_url(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _response_attempts(response: httpx.Response) -> int:
    extensions = getattr(response, "extensions", {})
    if not isinstance(extensions, dict):
        return 1
    metadata = extensions.get("request_metadata", {})
    if not isinstance(metadata, dict):
        return 1
    return int(metadata.get("attempts", 1))


def _exception_attempts(exc: Exception) -> int:
    metadata = getattr(exc, "request_metadata", {})
    if not isinstance(metadata, dict):
        return 0
    return int(metadata.get("attempts", 0))


def _response_retries(response: httpx.Response) -> int:
    extensions = getattr(response, "extensions", {})
    if not isinstance(extensions, dict):
        return 0
    metadata = extensions.get("request_metadata", {})
    if not isinstance(metadata, dict):
        return 0
    return int(metadata.get("retries", max(0, int(metadata.get("attempts", 1)) - 1)))


def _exception_retries(exc: Exception) -> int:
    metadata = getattr(exc, "request_metadata", {})
    if not isinstance(metadata, dict):
        return 0
    return int(metadata.get("retries", max(0, int(metadata.get("attempts", 0)) - 1)))


def _auto_fallback_enabled(
    llm_config: dict, capability: str, local_setting: Any = None
) -> bool:
    setting = local_setting
    if setting is None:
        setting = llm_config.get(f"{capability}_fallback")
    if setting is None and capability == "reasoning_effort":
        setting = llm_config.get("reasoning_fallback")
    if setting is None:
        fallback = llm_config.get("capability_fallback", "auto")
        setting = (
            fallback.get(capability, "auto")
            if isinstance(fallback, dict)
            else fallback
        )
    return setting is True or str(setting).lower() == "auto"


def _explicit_unsupported_parameter(
    response: httpx.Response, sent_parameters: tuple[str, ...]
) -> str | None:
    if response.status_code not in (400, 422):
        return None
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return None

    error = payload.get("error", payload) if isinstance(payload, dict) else payload
    text = str(error).lower()
    explicitly_unsupported = any(marker in text for marker in _UNSUPPORTED_MARKERS)
    if not explicitly_unsupported:
        return None

    parameter = error.get("param") if isinstance(error, dict) else None
    if parameter in sent_parameters:
        return str(parameter)

    for candidate in ("reasoning_effort", "temperature", "top_p"):
        if candidate in sent_parameters and candidate.lower() in text:
            return candidate
    return None


def _provider_request_id(response: httpx.Response, data: dict) -> str | None:
    for header in ("x-request-id", "request-id", "x-openai-request-id"):
        value = response.headers.get(header)
        if value:
            return value
    request_id = data.get("id")
    return str(request_id) if request_id else None
