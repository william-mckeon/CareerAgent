# careeragent-memory — model proxy is elsewhere; this image is a thin retrieval gateway.
FROM python:3.11-slim

# --- runtime user (drop root; a container escape should not be root on the host) ---
RUN groupadd --gid 1000 careeragent \
    && useradd --uid 1000 --gid 1000 --create-home careeragent

WORKDIR /app

# --- dependencies (separate layer for build-cache friendliness) ---
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# --- source ---
COPY src/ /app/src/

# backend.api / client.infra / store / retrieval / schemas / security all resolve from here
ENV PYTHONPATH=/app/src \
    MEMORY_PORT=8004 \
    PYTHONUNBUFFERED=1

EXPOSE 8004

USER careeragent

# Authenticated probe: sends X-API-Key and checks the reported status, so Docker's
# view of "healthy" matches the service's self-reported state (not just HTTP 200).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,json,urllib.request; \
req=urllib.request.Request('http://localhost:8004/health', headers={'X-API-Key': os.environ.get('MEMORY_API_KEY','')}); \
d=json.load(urllib.request.urlopen(req, timeout=4)); \
sys.exit(0 if d.get('status')=='ok' else 1)" || exit 1

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8004"]
