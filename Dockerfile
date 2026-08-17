# syntax=docker/dockerfile:1

# Build and runtime share one image; the three roles (api, worker, beat) differ
# only by command, so they can never drift apart in dependencies.
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libpq for psycopg, curl for the container healthcheck. Build tooling is not
# installed: every dependency ships a wheel for this platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies are installed from the manifest alone, so a source-only change
# reuses this layer.
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip && pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

# Runs unprivileged: a control plane holding vCenter credentials should not be
# root inside its own container.
RUN useradd --create-home --uid 10001 hostplane \
    && chown -R hostplane:hostplane /app
USER hostplane

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
