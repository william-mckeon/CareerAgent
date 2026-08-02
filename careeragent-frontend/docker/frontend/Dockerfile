# ============================================================================
# careeragent-frontend - Dockerfile
# Maintainer: William McKeon
# ============================================================================
#
# ROLE:
#   Builds the careeragent-frontend Docker image. Runs the Streamlit UI defined
#   in src/frontend/app.py and serves it on port 8501 inside the container.
#   docker-compose.yml maps container 8501 → host 8000 per the CareerAgent port
#   topology:
#
#     User → careeragent-frontend (:8000) → careeragent-api (:8001) → careeragent-infra (:8002) → BYOC provider
#
#   This container ONLY talks to careeragent-api. It does not authenticate
#   against, or even know the URL of, careeragent-infra or the compute provider.
#   The careeragent-api gateway owns those boundaries.
#
# ARCHITECTURE:
#
#   Browser
#     │
#     │ host port 8000 → container port 8501
#     ▼
#   careeragent-frontend (THIS CONTAINER)
#     │   runs:  streamlit run src/frontend/app.py
#     │   ships: src/frontend/app.py           — Streamlit UI
#     │          src/frontend/sse_decoder.py   — SSE / ChatCompletion decoder
#     │          src/frontend/conversations.py — conversation sidebar + state
#     │                                          (drives careeragent-sessions)
#     │   env:   CAREERAGENT_API_URL, CAREERAGENT_API_KEY (injected at runtime)
#     │
#     │ HTTP POST /chat   (SSE stream, X-API-Key: CAREERAGENT_API_KEY)
#     │ HTTP GET  /health (readiness poll, X-API-Key: CAREERAGENT_API_KEY)
#     ▼
#   careeragent-api (separate repo, separate container, port 8001)
#     │   owns: persona (bio.txt), auth chain, SSE relay
#     │
#     ▼
#   careeragent-infra (separate repo, separate container, port 8002)
#     │
#     ▼
#   BYOC compute provider — base reasoning model
#
# OWNERSHIP BOUNDARY:
#
#   This image SHIPS:
#     - src/frontend/app.py           — Streamlit UI, in-session state, chat
#                                       rendering, error display
#     - src/frontend/sse_decoder.py   — JSON ChatCompletion chunk decoder that
#                                       turns the SSE byte stream into typed
#                                       SSEEvent objects
#     - src/frontend/conversations.py — multi-conversation sidebar + state;
#                                       drives careeragent-sessions' conversation
#                                       CRUD (list / switch / new / delete)
#
#   This image does NOT SHIP:
#     - src/prompt/bio.txt — the persona is owned by careeragent-api and
#                            prepended server-side on every /chat request.
#                            careeragent-frontend does not load it, does not
#                            send a system message, and does not have a copy
#                            in the image at all.
#
# NETWORKING:
#   The frontend container only talks to careeragent-api. CAREERAGENT_API_URL
#   configures where to find the gateway. Common values:
#
#     Local dev, frontend in Docker, careeragent-api on host:
#       CAREERAGENT_API_URL=http://host.docker.internal:8001
#
#     Shared Docker network, careeragent-api reachable by service name:
#       CAREERAGENT_API_URL=http://careeragent-api:8001
#
#     External deployment:
#       CAREERAGENT_API_URL=https://api.your-domain.com
#
# REQUIREMENTS:
#   Uses the single requirements.txt at the repository root — the same file
#   that local developers use outside Docker. Intentionally lean: only
#   streamlit, requests, python-dotenv. NO backend deps (torch, transformers,
#   fastapi, sqlalchemy, httpx) are pulled into this image. The frontend is
#   an HTTP client of careeragent-api, not a co-located gateway.
#
# ENVIRONMENT VARIABLES (injected at runtime, NOT baked into image):
#   CAREERAGENT_API_URL — full URL of careeragent-api. See .env.example.
#   CAREERAGENT_API_KEY — shared secret for X-API-Key header. See .env.example.
#
#   These are runtime-only. They are passed in by docker-compose.yml via
#   `env_file: .env`, NOT via ENV directives in this Dockerfile. That keeps
#   secrets out of image layers and makes the same image reusable across dev,
#   staging, and production with different keys.
#
#   CAREERAGENT_API_KEY is the FRONTEND ↔ CAREERAGENT-API boundary key. It is NOT
#   the same as INFRA_API_KEY (which lives in careeragent-api and authenticates
#   to careeragent-infra) or PROVIDER_API_KEY (which lives in careeragent-infra
#   and authenticates to the compute provider). Defense in depth — see
#   careeragent-api's security model documentation.
#
# STREAMLIT CONFIGURATION:
#   --server.port 8501            — standard Streamlit port. Mapped to host
#                                   port 8000 in compose.
#   --server.address 0.0.0.0      — bind to all interfaces so Docker can
#                                   forward the port to the host.
#   --server.headless true        — disables the browser auto-open that
#                                   Streamlit attempts on first run.
#                                   Meaningless inside a container but
#                                   suppresses a harmless warning log line at
#                                   startup.
#   --server.fileWatcherType none — disables Streamlit's inotify file watcher.
#                                   Not needed inside a container (no live
#                                   edits) and can cause high CPU usage on
#                                   some Docker Desktop configurations.
#   --server.maxUploadSize 10     — cap resume uploads (P5) at 10 MB so a
#                                   hostile large file can't exhaust memory in
#                                   the widget before careeragent-fetch's own
#                                   hard byte cap even sees it. Streamlit's
#                                   default is 200 MB — far too permissive for
#                                   a service that only ingests a 1–3 page CV.
#
# BUILD:
#   From the repository root:
#     docker build -f docker/frontend/Dockerfile -t careeragent-frontend:1.0.0 .
#
# RUN (standalone, for quick local testing against a local careeragent-api):
#   docker run -it --rm \
#     -p 8000:8501 \
#     -e CAREERAGENT_API_URL="http://host.docker.internal:8001" \
#     -e CAREERAGENT_API_KEY="your_careeragent_api_key_here" \
#     careeragent-frontend:1.0.0
#
#   On Linux host.docker.internal requires --add-host=host.docker.internal:host-gateway
#
# DOCKER COMPOSE (recommended — this is the normal launch path):
#   docker-compose up -d
#   Then open: http://localhost:8000
# ============================================================================

# Base image — Python 3.11 slim. Small (~45 MB compressed), sufficient for a
# pure-Python Streamlit app. No CUDA, no build tools, no BLAS. Matches
# careeragent-api's base image for consistency across the family.
FROM python:3.11-slim

# ----------------------------------------------------------------------------
# METADATA
# ----------------------------------------------------------------------------
# These LABELs are visible via `docker inspect <image>` and surface in image
# registries. Keep them current so operators can identify what they are
# running without pulling the image apart.
LABEL maintainer="William McKeon"
LABEL description="careeragent-frontend — Streamlit UI on top of careeragent-api"
LABEL version="1.0.0"
LABEL license="Apache-2.0"
LABEL backend="careeragent-api"

# ----------------------------------------------------------------------------
# ENVIRONMENT CONFIGURATION
# ----------------------------------------------------------------------------

# Prevent Python from writing .pyc files to disk inside the container. They
# serve no purpose in an ephemeral container and only bloat the image layer
# cache.
ENV PYTHONDONTWRITEBYTECODE=1

# Prevent Python from buffering stdout/stderr so that every print() and
# logging call appears in `docker-compose logs` immediately. This is critical
# for debugging long-running streaming operations where a buffered delay
# would make the log feel frozen.
ENV PYTHONUNBUFFERED=1

# Set working directory. All COPY paths below are relative to this and all
# runtime paths in app.py assume this root.
WORKDIR /app

# ----------------------------------------------------------------------------
# DEPENDENCIES
# ----------------------------------------------------------------------------
# Copy the single requirements.txt from the repository root and install.
#
# File is copied BEFORE source code so that Docker's layer cache can skip the
# expensive pip install step on most rebuilds — it only re-runs when
# requirements.txt itself changes. Changes to app.py or sse_decoder.py
# invalidate only the later source-copy layer, not this one.
#
# --no-cache-dir on both commands prevents pip from keeping its wheel cache in
# the image, shaving ~50-100 MB off the final image size.

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ----------------------------------------------------------------------------
# APPLICATION SOURCE CODE
# ----------------------------------------------------------------------------
# One source tree is copied into the image:
#
#   src/frontend/   — the Streamlit app (app.py) plus the modules it imports:
#                     the SSE decoder (sse_decoder.py) and the conversation
#                     manager (conversations.py). COPYing the whole directory
#                     ships any new sibling module automatically.
#
# The src/prompt/ directory is intentionally NOT copied because it does not
# exist in this repo. The persona (bio.txt) is owned by careeragent-api and
# prepended server-side on every /chat request — careeragent-frontend does not
# carry it.
#
# Nothing else — no docs, no tests, no docker/ tree itself, no .env files.
# See .dockerignore for the exclusion rules applied to the build context
# (which also block .env, .git, caches, etc.).
#
# The src/frontend/ tree lands under /app/src/frontend/ matching the repo
# layout, so the relative import inside app.py (`from sse_decoder import ...`)
# resolves correctly because Streamlit adds the script's directory to
# sys.path at runtime.

COPY src/frontend/ ./src/frontend/

# ----------------------------------------------------------------------------
# EXPOSE PORT
# ----------------------------------------------------------------------------
# Declare the port Streamlit listens on INSIDE the container.
# docker-compose.yml maps host port 8000 → container port 8501, which
# satisfies the CareerAgent port topology:
#
#   User → careeragent-frontend (:8000) → careeragent-api (:8001) → careeragent-infra (:8002) → BYOC provider
#
# EXPOSE is informational — it does not actually publish the port. It
# documents the intent and enables PaaS tooling (Render, Railway, Fly.io) to
# auto-detect the service port when deploying this image without a compose
# file.

EXPOSE 8501

# ----------------------------------------------------------------------------
# NON-ROOT USER
# ----------------------------------------------------------------------------
# Drop root before launching. Running the container as a dedicated unprivileged
# user limits the blast radius if the Streamlit process is ever compromised —
# it cannot write outside its own files or escalate inside the container. The
# image needs no root capabilities at runtime: it only binds an unprivileged
# port (8501) and reads its own source tree.
#
# Create a system user/group with a fixed UID/GID (10001) for predictability
# across rebuilds, hand ownership of the app tree to it, then switch to it. All
# instructions AFTER this line (only the ENTRYPOINT) run as appuser.
RUN groupadd --system --gid 10001 appuser && \
    useradd --system --uid 10001 --gid 10001 --home-dir /app appuser && \
    chown -R appuser:appuser /app

USER appuser

# ----------------------------------------------------------------------------
# ENTRYPOINT
# ----------------------------------------------------------------------------
# Launch Streamlit.
#
# Exec form (single-line JSON array) is used so that Docker signals (SIGTERM
# from `docker stop`, SIGINT from Ctrl-C) reach the Streamlit process directly
# instead of being eaten by an intermediate shell. This enables clean,
# graceful shutdown on `docker-compose down`.
#
# ┌─────────────────────────────────────────────────────────────────┐
# │  DO NOT SPLIT THIS ENTRYPOINT ACROSS MULTIPLE LINES.            │
# │                                                                 │
# │  Multi-line JSON arrays are NOT valid Dockerfile syntax. The    │
# │  build will fail with a JSON parse error at this instruction.   │
# └─────────────────────────────────────────────────────────────────┘
#
# Argument breakdown:
#   streamlit run                   — start the Streamlit server
#   src/frontend/app.py             — application entry point
#   --server.port 8501              — listen port (must match EXPOSE)
#   --server.address 0.0.0.0        — bind all interfaces (for forward)
#   --server.headless true          — suppress browser-open attempt
#   --server.fileWatcherType none   — disable inotify watcher (unused
#                                     inside container; cuts idle CPU)

ENTRYPOINT ["streamlit", "run", "src/frontend/app.py", "--server.port", "8501", "--server.address", "0.0.0.0", "--server.headless", "true", "--server.fileWatcherType", "none", "--server.maxUploadSize", "10"]

# ============================================================================
# END OF DOCKERFILE
# ============================================================================
