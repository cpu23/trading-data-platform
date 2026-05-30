FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./

ENV UV_PYTHON=/usr/local/bin/python3
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

COPY . .

ENV PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /var/log/trading-data

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
