FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON=/usr/local/bin/python3 \
    UV_LINK_MODE=copy

WORKDIR /build/api
COPY api/pyproject.toml api/uv.lock ./
RUN uv sync --frozen --no-dev

WORKDIR /build/orchestrator
COPY orchestrator/pyproject.toml orchestrator/uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /build/api/.venv /app/api/.venv
COPY --from=builder /build/orchestrator/.venv /app/orchestrator/.venv
COPY api /app/api
COPY orchestrator /app/orchestrator
COPY config /app/config
COPY prompts /app/prompts
COPY db/migrations /app/db/migrations
RUN mkdir -p /var/log/trading-data /var/lib/trading-data/news

# Compose owns each production process and overrides this neutral image default.
CMD ["python3", "--version"]
