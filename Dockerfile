# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder-base

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --no-editable

COPY osmsg /app/osmsg
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

RUN find /app/.venv -type d -name __pycache__ -exec rm -rf {} +


FROM builder-base AS builder-distroless
RUN sed -i 's|^home = .*|home = /usr/bin|' /app/.venv/pyvenv.cfg \
    && rm -f /app/.venv/bin/python /app/.venv/bin/python3 /app/.venv/bin/python3.13 \
    && ln -s /usr/bin/python3.13 /app/.venv/bin/python3.13 \
    && ln -s python3.13 /app/.venv/bin/python3 \
    && ln -s python3.13 /app/.venv/bin/python


FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder-api

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --group api

COPY osmsg /app/osmsg
COPY api /app/api
# --no-editable so osmsg is copied into site-packages, not linked to /app/osmsg (absent in the final api stage).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group api --no-editable

RUN find /app/.venv -type d -name __pycache__ -exec rm -rf {} +


FROM gcr.io/distroless/python3-debian13:nonroot AS cli

WORKDIR /work
COPY --from=builder-distroless --chown=nonroot:nonroot /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/.venv/bin/osmsg"]
CMD ["--help"]


FROM python:3.13-slim AS api

# osmsg pulls in osmium (via the shared query surface), which needs libexpat at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder-api /app/.venv /app/.venv
COPY api /app/api

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000
ENTRYPOINT ["/app/.venv/bin/litestar", "--app", "api.app:app", "run", "--host", "0.0.0.0", "--port", "8000"]
CMD []


FROM python:3.13-slim AS worker

RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

ARG SUPERCRONIC_VERSION=0.2.33
ARG TARGETARCH=amd64
ADD --chmod=755 https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH} /usr/local/bin/supercronic

WORKDIR /app
COPY --from=builder-base /app/.venv /app/.venv
COPY worker-entrypoint.sh /usr/local/bin/worker-entrypoint.sh
RUN chmod +x /usr/local/bin/worker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OSMSG_OUTPUT_DIR=/var/lib/osmsg \
    OSMSG_CACHE_DIR=/var/cache/osmsg

RUN mkdir -p /var/lib/osmsg /var/cache/osmsg

ENTRYPOINT ["/usr/local/bin/worker-entrypoint.sh"]
