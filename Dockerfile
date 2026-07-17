FROM python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS builder

COPY --from=ghcr.io/astral-sh/uv@sha256:a5727064a0de127bdb7c9d3c1383f3a9ac307d9f2d8a391edc7896c54289ced0 /uv /usr/local/bin/uv
ENV UV_PYTHON=/usr/local/bin/python3 \
    UV_LINK_MODE=copy

WORKDIR /build/api
COPY api/pyproject.toml api/uv.lock ./
RUN uv sync --frozen --no-dev

WORKDIR /build/orchestrator
COPY orchestrator/pyproject.toml orchestrator/uv.lock ./
RUN uv sync --frozen --no-dev

FROM python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app
COPY --from=builder /build/api/.venv /app/api/.venv
COPY --from=builder /build/orchestrator/.venv /app/orchestrator/.venv
COPY api /app/api
COPY orchestrator /app/orchestrator
COPY config /app/config
COPY prompts /app/prompts
COPY db/migrations /app/db/migrations
RUN groupadd --gid 10001 trading-data \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin trading-data \
    && mkdir -p /var/log/trading-data /var/lib/trading-data/news \
    && chown -R 10001:10001 /var/log/trading-data /var/lib/trading-data /app

USER 10001:10001

# Compose owns each production process and overrides this neutral image default.
CMD ["python3", "--version"]
