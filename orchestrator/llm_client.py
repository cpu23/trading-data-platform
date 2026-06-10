import time

import httpx

from http_client import make_request
from logging_config import get_logger

logger = get_logger("llm_client")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def call_llm(
    prompt: str,
    model: str | None = None,
    temperature: float | None = None,
    max_retries: int = 3,
    correlation_id: str | None = None,
    config: dict | None = None,
) -> dict:
    if config is None:
        from config_loader import load_config
        config = load_config()

    llm_config = config["llm"]
    api_key = llm_config["api_key"]
    effective_model = resolve_model(config, model=model)
    effective_temperature = temperature if temperature is not None else llm_config.get("temperature", 0.2)

    request_body = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": effective_temperature,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/trading-data-platform",
    }

    start_ms = time.monotonic() * 1000

    logger.info(
        "llm_call_started",
        action="llm_call",
        model=effective_model,
        prompt_length=len(prompt),
        correlation_id=correlation_id or "none",
    )

    try:
        response = make_request(
            method="POST",
            url=OPENROUTER_URL,
            headers=headers,
            json_body=request_body,
            timeout=120.0,
            max_retries=max_retries,
            correlation_id=correlation_id,
        )
        response.raise_for_status()
    except Exception as exc:
        duration_ms = int(time.monotonic() * 1000 - start_ms)
        logger.error(
            "llm_call_failed",
            action="llm_call",
            model=effective_model,
            error=str(exc),
            duration_ms=duration_ms,
            correlation_id=correlation_id or "none",
            prompt=prompt,
        )
        raise

    duration_ms = int(time.monotonic() * 1000 - start_ms)
    data = response.json()

    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    tokens_input = usage.get("prompt_tokens", 0)
    tokens_output = usage.get("completion_tokens", 0)

    model_used = data.get("model", effective_model)

    cost_usd = usage.get("cost")
    if cost_usd is not None:
        cost_usd = float(cost_usd)

    result = {
        "content": content,
        "model": model_used,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
    }

    logger.info(
        "llm_call_completed",
        action="llm_call",
        model=model_used,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        correlation_id=correlation_id or "none",
        response=content,
    )

    return result


def resolve_model(config: dict, processor_id: str | None = None, model: str | None = None) -> str:
    """Resolve an OpenRouter model without coupling processors to a provider model."""
    if model:
        return model
    llm_config = config.get("llm", {})
    if processor_id:
        override = llm_config.get("models", {}).get(processor_id)
        if override:
            return override
    return llm_config.get("default_model", "deepseek/deepseek-v4-flash")
