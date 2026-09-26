FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.6.11 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GROCERY_STATE_DIR=/app/state

RUN groupadd --system --gid 10001 grocery \
    && useradd --system --uid 10001 --gid grocery --home-dir /nonexistent --shell /usr/sbin/nologin grocery \
    && mkdir -p /app/state \
    && chown grocery:grocery /app/state \
    && chmod 0700 /app/state

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
USER 10001:10001
VOLUME ["/app/state"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
ENTRYPOINT ["grocery-mcp"]
