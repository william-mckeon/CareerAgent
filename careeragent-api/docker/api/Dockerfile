# ============================================================================
# careeragent-api - Dockerfile
# Maintainer: William McKeon
# The identity gateway for CareerAgent
# ============================================================================
#
# ROLE:
#   Builds the runtime image for the careeragent-api service. Produces a lean
#   pure-Python container with FastAPI + uvicorn + httpx and the persona
#   (bio.txt) baked in. No GPU, no CUDA, no compilers — this service does no
#   inference work. All it does is auth, identity injection, SSE relay between
#   careeragent-frontend and careeragent-infra, fire-and-forget event emission
#   to careeragent-logger, and — when careeragent-memory is configured — optional
#   session-scoped retrieval/ingest against careeragent-memory.
#
# IMAGE SIZE TARGET:
#   ~150-200 MB total. Pure-Python, slim base. Comparable to the
#   careeragent-frontend image and to careeragent-infra (which is also a thin
#   FastAPI proxy). The logger, infra, and memory clients add zero MB to the
#   image: they use Python stdlib (hmac, hashlib, json, asyncio, logging) for
#   signing, scheduling, and diagnostics and reuse the already-installed httpx
#   for HTTP, so no new pip packages, no new apt packages, and no new
#   compilers are needed.
#
# BASE IMAGE CHOICE:
#   python:3.11-slim — same Python major.minor as careeragent-frontend so the
#   two product-layer services run on identical interpreters. careeragent-infra
#   runs on python:3.12-slim per its own Dockerfile (it is also a pure-Python
#   proxy with no GPU stack). careeragent-api stays on 3.11 to match
#   careeragent-frontend rather than careeragent-infra because careeragent-api and
#   careeragent-frontend are deployed and operated together as the product layer.
#
# BUILD CONTEXT:
#   Build from the repo root:
#     docker build -f docker/api/Dockerfile -t careeragent-api:latest .
#   docker-compose handles this automatically via the build: directive
#   in docker-compose.yml.
#
# LAYER ORDERING:
#   Layers are ordered from least-frequently-changed to most-frequently
#   changed so the Docker layer cache reuses as much as possible on
#   rebuilds:
#     1. OS package install        — almost never changes
#     2. requirements.txt copy     — changes when deps change
#     3. pip install               — re-runs only when requirements.txt changes
#     4. bio.txt copy              — changes when the persona is rotated
#     5. src/client/ copy          — changes when an outbound client is
#                                    added or modified
#     6. application source copy   — changes on every code edit
#   This means a typical code-only change to api.py rebuilds in seconds
#   because layers 1-5 are pulled from the cache. Editing a client module
#   (client/logger.py, client/infra.py, or client/memory.py) invalidates
#   layers 5-6 only. Editing bio.txt invalidates 4-6.
#
# REBUILD TRIGGERS:
#   Changing src/backend/api.py    → fast rebuild (last COPY layer only)
#   Changing src/client/*.py        → rebuild last two COPY layers
#   Changing src/prompt/bio.txt     → rebuild last three COPY layers
#   Changing requirements.txt       → slower (pip install re-runs)
#   Changing this Dockerfile        → full rebuild
#
# NON-ROOT USER:
#   The container runs as a dedicated 'careeragent' user (uid 1000) rather
#   than root. This matches the security posture of the other services
#   and means a container escape does not give root on the host.
#
# HEALTHCHECK:
#   Docker-level HEALTHCHECK uses the same /health endpoint the frontend
#   polls. It supplies the X-API-Key header from the CAREERAGENT_API_KEY env
#   var because /health requires auth (per api.py design). curl is installed
#   minimally for this purpose.
#
#   /health requires only CAREERAGENT_API_KEY. The three logger env vars
#   (LOGGER_URL, LOGGER_API_KEY, LOGGER_HMAC_SECRET) are validated inside
#   api.py's lifespan startup; if any is missing the container refuses to
#   boot and HEALTHCHECK is never reached. If HEALTHCHECK reports healthy,
#   all six runtime secrets/URLs the gateway needs are by definition present
#   and valid. (The careeragent-memory env vars are optional and opt-in — they
#   are NOT part of this boot gate; when unset, the gateway forwards full
#   history exactly as before.) The /health endpoint's upstream probe is
#   handled by InfraClient.check_health(); the endpoint contract — auth
#   requirement, response shape, status vocabulary — is independent of that
#   internal call.
#
# PORT:
#   Container listens on 8001 (per the port convention in api.py's
#   architecture diagram: 8000=frontend, 8001=careeragent-api,
#   8002=careeragent-infra, 8003=careeragent-logger, 8004=careeragent-memory).
#   docker-compose.yml maps host 8001 → container 8001.
#
# RUNTIME ENV VARS EXPECTED FROM docker-compose.yml / .env:
#   CAREERAGENT_API_KEY    — inbound auth (validated by /chat and /health,
#                          also used by HEALTHCHECK in this Dockerfile)
#   INFRA_API_KEY        — outbound to careeragent-infra (same value as
#                          careeragent-infra's own API_KEY)
#   CAREERAGENT_INFRA_URL  — careeragent-infra base URL
#   LOGGER_URL           — careeragent-logger base URL
#   LOGGER_API_KEY       — outbound to careeragent-logger
#   LOGGER_HMAC_SECRET   — payload-signing secret
#   Plus optional tunables (CAREERAGENT_LOG_LEVEL, CAREERAGENT_FRONTEND_URL,
#   CAREERAGENT_UPSTREAM_CONNECT_TIMEOUT, CAREERAGENT_UPSTREAM_READ_TIMEOUT,
#   CAREERAGENT_HEALTH_TIMEOUT, CAREERAGENT_BIO_PATH,
#   CAREERAGENT_LOGGER_QUEUE_MAX_SIZE).
#   Plus optional careeragent-memory vars (memory is opt-in — unset by
#   default, and NOT required to boot):
#     MEMORY_URL              — careeragent-memory base URL
#     MEMORY_API_KEY          — outbound to careeragent-memory
#   MEMORY_URL + MEMORY_API_KEY together enable the optional memory boundary.
#   The rest tune it: MEMORY_SESSION_ID (scopes retrieve/ingest and populates
#   the logger's session_id), MEMORY_RECENT_N, MEMORY_TOP_K,
#   MEMORY_RETRIEVE_TIMEOUT.
# ============================================================================

# ----------------------------------------------------------------------------
# BASE IMAGE
# ----------------------------------------------------------------------------
# python:3.11-slim is a Debian-based slim image with Python 3.11 only and
# no extra tooling. Matches careeragent-frontend's base image so both product-
# layer services run on identical interpreters and base OS.
FROM python:3.11-slim

# ----------------------------------------------------------------------------
# IMAGE METADATA
# ----------------------------------------------------------------------------
# OCI labels make the image self-describing for registries, scanners, and
# operators running `docker inspect`.
LABEL org.opencontainers.image.title="careeragent-api"
LABEL org.opencontainers.image.description="Identity gateway for CareerAgent — SSE relay between careeragent-frontend and careeragent-infra, with fire-and-forget event emission to careeragent-logger"
LABEL org.opencontainers.image.vendor="William McKeon"
LABEL org.opencontainers.image.version="1.0.0"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# ----------------------------------------------------------------------------
# PYTHON RUNTIME ENVIRONMENT
# ----------------------------------------------------------------------------
# PYTHONDONTWRITEBYTECODE=1   Don't write .pyc files to disk — saves space
#                              and avoids stale bytecode warnings on rebuild.
# PYTHONUNBUFFERED=1          Don't buffer stdout — log lines appear in
#                              `docker logs` immediately, matching the
#                              behaviour the other two services rely on.
# PYTHONPATH=/app/src         Lets imports resolve as `from backend.api ...`,
#                              `from client.logger ...`, `from client.infra
#                              ...`, and `from client.memory ...` per the
#                              import path rule in api.py's header.
# PIP_NO_CACHE_DIR=1          pip doesn't cache wheels — keeps the image
#                              small. We won't reinstall in this container.
# PIP_DISABLE_PIP_VERSION_CHECK=1  Suppresses the pip self-update warning
#                              on every invocation.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ----------------------------------------------------------------------------
# OS PACKAGES
# ----------------------------------------------------------------------------
# We install only what the container truly needs at runtime:
#   curl   — required by the HEALTHCHECK directive below. Without it,
#            Docker's healthcheck cannot probe /health and the container
#            shows as unhealthy in `docker ps` even when fully working.
# ca-certificates is already present in python:3.11-slim, so we don't
# install it explicitly.
#
# `--no-install-recommends` keeps the install minimal. The apt cache is
# cleared in the same RUN layer to avoid bloating the image. No additional
# apt packages are needed: the logger, infra, and memory clients sign,
# schedule, and call out using Python stdlib (hmac, hashlib, json, asyncio,
# logging) and the already-present httpx — no cryptography wheel, no
# compilers, no OpenSSL beyond what python:3.11-slim already provides for
# httpx's TLS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------------
# WORKING DIRECTORY
# ----------------------------------------------------------------------------
# /app is the convention used by careeragent-infra and careeragent-frontend.
# Source code lives at /app/src/, matching the in-repo layout.
WORKDIR /app

# ----------------------------------------------------------------------------
# NON-ROOT USER
# ----------------------------------------------------------------------------
# Create the 'careeragent' user up front so we can chown directories as we
# create them. uid/gid 1000 is the conventional first non-system user on
# Debian and matches host-side defaults so volume permissions work without
# surprises.
#
# We don't switch to this user yet — installs as root, then USER below
# drops privilege for the runtime.
RUN groupadd --system --gid 1000 careeragent \
    && useradd --system --uid 1000 --gid careeragent --create-home --shell /bin/bash careeragent

# ----------------------------------------------------------------------------
# PYTHON DEPENDENCIES
# ----------------------------------------------------------------------------
# Copy requirements.txt FIRST and install in its own layer so changes to
# application source don't invalidate the pip-install cache. This is the
# single biggest reason rebuilds are fast. The logger, infra, and memory
# clients reuse the existing httpx dependency; HMAC signing uses Python
# stdlib (hmac, hashlib, json) which ships with python:3.11-slim.
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt

# ----------------------------------------------------------------------------
# PERSONA
# ----------------------------------------------------------------------------
# bio.txt is baked into the image at build time per the design decision in
# api.py — it is loaded once at startup by load_identity() and prepended as
# the first system message on every /chat call.
#
# Path inside the container is /app/src/prompt/bio.txt, which matches the
# default CAREERAGENT_BIO_PATH in api.py's Config class. Operators who want to
# override this can mount a volume over /app/src/prompt/ in
# docker-compose.yml or set CAREERAGENT_BIO_PATH to a different path.
COPY src/prompt/ /app/src/prompt/

# ----------------------------------------------------------------------------
# OUTBOUND HTTP CLIENTS
# ----------------------------------------------------------------------------
# src/client/ holds the modules careeragent-api uses to talk to upstream
# services. Three clients live here:
#   client/logger.py  — fire-and-forget event emission to careeragent-logger
#   client/infra.py   — careeragent-infra streaming relay + health proxy
#   client/memory.py  — careeragent-memory retrieval + ingest (optional)
# The single `COPY src/client/ /app/src/client/` directive below picks up
# all three files because it copies the entire directory; further clients
# added to the directory are picked up automatically, with no Dockerfile
# change. (client/memory.py is itself an instance of that: it was added with
# no edit to this directive.)
#
# Placed BEFORE the src/backend/ COPY because client modules change less
# frequently than the FastAPI app. A typical edit to api.py invalidates only
# the next COPY layer; src/client/ stays cached. Conversely, an edit to a
# client module invalidates this layer and the next one, leaving everything
# before this point cached.
COPY src/client/ /app/src/client/

# ----------------------------------------------------------------------------
# AGENT (the resume-coach tool loop)
# ----------------------------------------------------------------------------
# src/agent/ holds the agentic loop, tool catalog, permission engine, and
# prompt assembly — active only when careeragent-dossier is configured
# (AGENT_ENABLED). Copied as a whole directory so new agent modules are picked
# up with no Dockerfile change. Placed before src/backend/ because it changes
# less often than api.py itself.
COPY src/agent/ /app/src/agent/

# ----------------------------------------------------------------------------
# APPLICATION SOURCE
# ----------------------------------------------------------------------------
# Copy the FastAPI app last so a typical edit to api.py only invalidates this
# single layer. Subsequent rebuilds reuse all preceding layers from cache.
#
# We deliberately do NOT use a wildcard `COPY src/ /app/src/` because that
# would couple bio.txt, the client modules, and the backend source into a
# single cache layer, defeating the layering. Three explicit COPY directives
# keep them independently invalidated.
COPY src/backend/ /app/src/backend/

# ----------------------------------------------------------------------------
# OWNERSHIP
# ----------------------------------------------------------------------------
# Hand /app to the non-root user so the runtime process can read its own
# files. Done in one layer at the end so the chown happens after every COPY,
# ensuring no root-owned files remain.
RUN chown -R careeragent:careeragent /app

# ----------------------------------------------------------------------------
# DROP PRIVILEGES
# ----------------------------------------------------------------------------
# From here on, every process in the container runs as 'careeragent'.
USER careeragent

# ----------------------------------------------------------------------------
# NETWORK
# ----------------------------------------------------------------------------
# Document the listening port. EXPOSE is informational — it does not publish
# the port to the host. docker-compose.yml does that. Port 8001 per the
# architecture diagram in api.py.
EXPOSE 8001

# ----------------------------------------------------------------------------
# HEALTHCHECK
# ----------------------------------------------------------------------------
# Docker probes /health every 30 seconds once the container has been running
# for 10 seconds. Three consecutive failures mark the container unhealthy.
#
# /health requires the X-API-Key header (per api.py design) so the probe
# pulls CAREERAGENT_API_KEY from the container environment. The env var is set
# by docker-compose.yml from .env, so it is available inside the container by
# the time HEALTHCHECK fires.
#
# Why not just check that the port is open? Because that would mark the
# container healthy even when careeragent-infra is unreachable and the gateway
# is effectively useless. /health does a real upstream probe (via
# InfraClient.check_health()) and returns `status: ok | loading | unreachable`
# — exactly the signal we want.
#
# We accept any status the endpoint reports as "healthy from careeragent-api's
# perspective" because the container itself is functioning correctly even
# when careeragent-infra is loading (e.g. a serverless provider worker
# cold-starting). The frontend's gate-open loop handles the ok/loading
# distinction at the user-facing layer.
#
# HEALTHCHECK requires only CAREERAGENT_API_KEY. The three logger env vars
# (LOGGER_URL, LOGGER_API_KEY, LOGGER_HMAC_SECRET) are validated in api.py's
# lifespan startup — if any is missing, the container refuses to boot and
# this HEALTHCHECK is never reached. Therefore a passing HEALTHCHECK implies
# all six runtime secrets/URLs are present and the container has successfully
# wired up both the LoggerClient and the InfraClient. (The MemoryClient is
# optional — it is wired up only when MEMORY_URL + MEMORY_API_KEY are set,
# and its absence does not affect the boot gate or this healthcheck.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --silent --fail \
        --header "X-API-Key: ${CAREERAGENT_API_KEY}" \
        http://localhost:8001/health \
        || exit 1

# ----------------------------------------------------------------------------
# ENTRYPOINT
# ----------------------------------------------------------------------------
# Run uvicorn directly (not via shell) so signals reach the Python process
# unchanged. SIGTERM from `docker stop` triggers FastAPI's lifespan shutdown
# handler in api.py, which stops the clients in order (memory → logger →
# infra):
#   - Stops the MemoryClient first when memory is configured (draining
#     in-flight ingest tasks) so any ingest-failure events can still reach a
#     live logger during the drain
#   - Stops the LoggerClient (draining its queue with timeout)
#   - Stops the InfraClient (closing its internal httpx.AsyncClient cleanly)
# These stops happen during the lifespan shutdown sequence so any pending
# logger events get a final drain attempt and the careeragent-infra connection
# pool is released before the process exits.
#
# --host 0.0.0.0  Bind to all interfaces inside the container so
#                 docker-compose's port mapping reaches it.
# --port 8001     Match EXPOSE above and the architecture diagram.
# --no-access-log Disable uvicorn's per-request access log because api.py
#                 emits its own structured INFO line per /chat call. Two log
#                 lines per request would be noise. Application logs
#                 (logger.info / .warning / .error) still flow through.
#
# We deliberately do NOT pass --proxy-headers. There is no reverse proxy in
# this stack — careeragent-frontend talks to careeragent-api directly. With
# --proxy-headers and no --forwarded-allow-ips restriction, a direct client
# could spoof X-Forwarded-* (e.g. forge client.host), so without a trusted
# proxy in front the flag only adds attack surface for no benefit. If a real
# proxy is introduced later, re-add "--proxy-headers" together with a scoped
# "--forwarded-allow-ips=<proxy-ip>".
CMD ["uvicorn", "backend.api:app", \
     "--host", "0.0.0.0", \
     "--port", "8001", \
     "--no-access-log"]

# ============================================================================
# END OF FILE
# ============================================================================