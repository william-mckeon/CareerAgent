# =============================================================================
# docker/jobs/Dockerfile
#
# careeragent-jobs — the background/async job runner. A pure-Python async
# FastAPI service (FastAPI + uvicorn + SQLAlchemy/asyncpg + httpx) plus an
# in-process worker task. No GPU/ML, no build toolchain: every dependency ships
# a manylinux wheel for cp311, so NO apt layer is needed. Mirrors the house
# pattern: slim base, non-root user, stdlib healthcheck, flat import layout via
# PYTHONPATH=/app/src.
# =============================================================================
FROM python:3.11-slim

LABEL maintainer="William McKeon"
LABEL description="careeragent-jobs — background/async job runner for CareerAgent"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONPATH=/app/src \
    JOBS_PORT=8011

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Application source.
COPY src/ ./src/

# Drop root (fixed uid 1000).
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser && \
    chown -R appuser:appuser /app
USER appuser

# Stdlib healthcheck — /health needs no auth and no extra package.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8011/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8011", "--app-dir", "/app/src"]
