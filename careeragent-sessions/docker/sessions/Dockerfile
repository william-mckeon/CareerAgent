# =============================================================================
# docker/sessions/Dockerfile
#
# careeragent-sessions — the conversation system-of-record. A pure-Python async
# FastAPI service (FastAPI + uvicorn + SQLAlchemy/asyncpg + httpx). No GPU/ML.
# Mirrors the house pattern: slim base, non-root user, stdlib healthcheck, flat
# import layout via PYTHONPATH=/app/src.
# =============================================================================
FROM python:3.12-slim

LABEL maintainer="William McKeon"
LABEL description="careeragent-sessions — conversation system-of-record for CareerAgent"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONPATH=/app/src \
    SESSIONS_PORT=8005

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Application source.
COPY src/ ./src/

# Drop root.
RUN useradd --create-home --shell /usr/sbin/nologin appuser && \
    chown -R appuser:appuser /app
USER appuser

# Stdlib healthcheck — /health needs no auth and no extra package.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8005/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8005"]
