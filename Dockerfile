# Chainguard's rolling glibc Python image (digest pinned) supports the
# orchestrator's manylinux-only inference runtime while publishing a package
# set with no HIGH/CRITICAL findings. ``apk upgrade`` applies security point
# releases published after this digest without weakening the scan policy.
FROM cgr.dev/chainguard/python@sha256:cd42e3e78f19faffe161fccf60af83503ee3851dd12efdae7d2488148e2fcd49 AS builder

USER 0

COPY --from=ghcr.io/astral-sh/uv@sha256:a5727064a0de127bdb7c9d3c1383f3a9ac307d9f2d8a391edc7896c54289ced0 /uv /usr/local/bin/uv
ENV UV_PYTHON=/usr/bin/python3 \
    UV_LINK_MODE=copy

WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

FROM cgr.dev/chainguard/python@sha256:cd42e3e78f19faffe161fccf60af83503ee3851dd12efdae7d2488148e2fcd49
USER 0
RUN apk update \
    && apk upgrade --no-cache \
    && apk add --no-cache poppler-utils tesseract tesseract-eng tesseract-osd tzdata

WORKDIR /app
COPY --from=builder /build/.venv /app/.venv
COPY api /app/api
COPY orchestrator /app/orchestrator
COPY contracts /app/contracts
COPY config /app/config
COPY prompts /app/prompts
COPY db/schema.sql /app/db/schema.sql
RUN addgroup --gid 10001 trading-data \
    && adduser --uid 10001 --ingroup trading-data --disabled-password \
        --no-create-home --shell /sbin/nologin trading-data \
    && mkdir -p /var/log/trading-data /var/lib/trading-data/news /app/state \
    && chown -R 10001:10001 /var/log/trading-data /var/lib/trading-data \
    && chown 10001:10001 /app/state \
    && chmod 700 /app/state

# Image artifacts (code, config, prompts, schema, venv) stay root-owned
# and read-only for the runtime user; only the state and data paths above are
# writable. Bytecode caching would otherwise try to write under /app and is
# useless in an immutable image anyway.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/orchestrator:/app/api \
    PATH="/app/.venv/bin:$PATH"

USER 10001:10001

# Clear the base image's Python entrypoint: Compose supplies each service's
# complete executable path.
ENTRYPOINT []

# Compose owns each production process and overrides this neutral image default.
CMD ["python3", "--version"]
