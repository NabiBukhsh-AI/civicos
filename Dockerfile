# syntax=docker/dockerfile:1.7
#
# Multi-stage build. The runtime stage carries no build toolchain, runs as a
# non-root user, and ships only the installed package - the smallest surface a
# municipality's IT department has to accept.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# A virtualenv keeps the runtime copy to a single, self-contained directory.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel \
 && pip install ".[postgres,redis,anthropic,google,openai,storage]"


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CIVICOS_ENVIRONMENT=production \
    CIVICOS_OBSERVABILITY__LOG_FORMAT=json

# curl is used by the container healthcheck; libpq for asyncpg's runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1001 civicos \
 && useradd --system --uid 1001 --gid civicos --create-home civicos

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=civicos:civicos alembic.ini ./
COPY --chown=civicos:civicos migrations ./migrations
COPY --chown=civicos:civicos src ./src

RUN mkdir -p /app/var/uploads && chown -R civicos:civicos /app/var

USER civicos
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

# Migrations are NOT run here: schema changes are an operator decision, not a
# side effect of a container restart. Run `civicos db upgrade` as a job.
CMD ["uvicorn", "civicos.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
