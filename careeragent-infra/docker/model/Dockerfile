# =============================================================================
# docker/model/Dockerfile
#
# CareerAgent Inference Proxy Container
#
# Design decisions
# ---------------------------------------
# 1. python:3.12-slim base image — with the base model, the nervous-system
#    model, and the embedding model all running on Amazon Bedrock,
#    this container only runs the FastAPI proxy layer
#    (src/api/main.py). There is no local GPU, no model loading, no CUDA stack
#    needed here. The image stays under 500 MB.
#
# 2. No CUDA, no PyTorch, no GPU-related dependencies — all inference happens
#    on Amazon Bedrock. This container is a pure Python web application:
#    FastAPI + uvicorn + litellm (+ boto3 for the AWS credential chain).
#
# 3. No volume mount — no model weights live here. Nothing to pull, nothing
#    to mount.
#
# 4. No NVIDIA toolkit, no deploy.resources.reservations block needed in
#    docker-compose.yml — this container has no GPU dependency.
#
# 5. All secrets (API_KEY, AWS credentials) and tuneable values
#    (AWS_REGION_NAME, BASE_MODEL, NERVOUS_SYSTEM_MODEL, EMBEDDING_MODEL,
#    REASONING_EFFORT, MAX_TOKENS, REQUEST_TIMEOUT) are passed via the root
#    .env file — never hardcoded here.
#
# 6. This Dockerfile serves one service in docker-compose.yml:
#      careeragent-infra — runs the FastAPI proxy on port 8002.
#    The proxy validates X-API-Key auth on every request. On /chat it maps
#    reasoning_effort to Claude's thinking depth, routes to the correct Bedrock
#    model based on the optional model field, and streams the OpenAI-shaped SSE
#    response back. On /embed it embeds the input via the embedding model and
#    returns the OpenAI-shaped JSON response (no streaming).
#
# 7. Port topology:
#      careeragent-api (:8001) → careeragent-infra (:8002) → Bedrock base model           [/chat, default]
#                                                       → Bedrock nervous-system model [/chat, model="nervous_system"]
#                                                       → Bedrock embedding model      [/embed]
#    careeragent-infra is the only service in this repo. All models run on
#    Amazon Bedrock — no inference happens here. api is the
#    primary caller today; other server-side services may call the proxy as
#    the system grows.
#
# Build (from project root)
# -------------------------
#   docker build -f docker/model/Dockerfile -t careeragent-infra:latest .
#
# Run via docker-compose (recommended — see docker-compose.yml at root)
#   docker-compose up careeragent-infra
# =============================================================================


# -----------------------------------------------------------------------------
# Base image — python:3.12-slim
#
# Minimal Debian-based Python 3.12 image. No CUDA, no PyTorch, no GPU tooling —
# none of that is needed here. The proxy is a pure async Python web app.
# Pinned to 3.12-slim for reproducibility.
# -----------------------------------------------------------------------------
FROM python:3.12-slim


# -----------------------------------------------------------------------------
# Labels — human-readable metadata, visible via `docker inspect`
# -----------------------------------------------------------------------------
LABEL maintainer="William McKeon"
LABEL description="CareerAgent inference proxy — routes requests to the base, nervous-system, and embedding models on Amazon Bedrock"


# -----------------------------------------------------------------------------
# Environment — system level
#
# PYTHONUNBUFFERED=1
#   Disables Python output buffering so every log line appears in
#   docker-compose logs immediately. Critical for streaming proxy visibility.
#
# PYTHONDONTWRITEBYTECODE=1
#   Stops Python writing .pyc files inside the container.
#
# PIP_NO_CACHE_DIR=1
#   Prevents pip storing its download cache inside the image layer.
#
# DEBIAN_FRONTEND=noninteractive
#   Suppresses apt interactive prompts during build.
# -----------------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive


# -----------------------------------------------------------------------------
# Environment — application level defaults
#
# These match the os.environ.get() calls in src/api/main.py. All are overridable
# via the root .env file without rebuilding the image. Non-secret tunables get
# sensible defaults here; secrets are NOT baked into the image — they arrive at
# runtime via env_file (.env).
#
# API_KEY
#   Secret validated against the X-API-Key header on every /chat and /embed
#   request. The same value careeragent-api holds as INFRA_API_KEY. Required —
#   set in .env (empty default here).
#
# AWS credentials + region (set in .env — NOT baked here)
#   AWS_REGION_NAME (required; falls back to AWS_REGION) plus AWS_ACCESS_KEY_ID /
#   AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN, OR an attached IAM role / shared
#   profile. These authenticate careeragent-infra to Amazon Bedrock and are read
#   directly by LiteLLM/boto3. AWS_REGION_NAME is given an empty default so the
#   variable name is declared; the secret keys are left to .env entirely.
#
# BASE_MODEL
#   LiteLLM Bedrock model id for the base (coach) model. Default route for all
#   /chat requests. Required (set in .env).
#   e.g. bedrock/us.anthropic.claude-opus-4-8
#
# NERVOUS_SYSTEM_MODEL
#   LiteLLM Bedrock model id for the fast control model. Used when
#   model="nervous_system". Optional — without it that route reports
#   "not configured" and omitting the model field always routes to base.
#
# EMBEDDING_MODEL
#   LiteLLM Bedrock model id for the embedding model. Used by POST /embed.
#   Optional — without it /embed returns "not configured" and /chat is
#   unaffected. e.g. bedrock/amazon.titan-embed-text-v2:0
#
# REASONING_EFFORT
#   Default reasoning effort for both chat models if not set per request (the
#   embedding model does not reason). Maps to Claude extended-thinking depth.
#   low | medium (default) | high. Overridable per request.
#
# MAX_TOKENS / REQUEST_TIMEOUT
#   Max output tokens per generation (default 8192) and per-request Bedrock
#   timeout in seconds (default 600). Bedrock is always-warm, so the timeout is
#   a real bound, not a cold-start absorber.
#
# MODEL_RETRIES / BACKOFF_CAP
#   Retry transient Bedrock errors (503 bursts / 429 throttles) with exponential
#   backoff + jitter, capped at BACKOFF_CAP seconds (defaults 5 retries / 20s).
#   Permanent errors (400 / access-denied / context overflow) fail fast.
# -----------------------------------------------------------------------------
ENV API_KEY="" \
    AWS_REGION_NAME="" \
    BASE_MODEL="" \
    NERVOUS_SYSTEM_MODEL="" \
    EMBEDDING_MODEL="" \
    REASONING_EFFORT="medium" \
    MAX_TOKENS="8192" \
    REQUEST_TIMEOUT="600" \
    MODEL_RETRIES="5" \
    BACKOFF_CAP="20" \
    INFRA_ENABLE_DOCS=""


# -----------------------------------------------------------------------------
# Working directory
# -----------------------------------------------------------------------------
WORKDIR /app


# -----------------------------------------------------------------------------
# Python dependencies
#
# requirements.txt is copied before src/ so Docker can cache this layer.
# A code-only change will not re-run pip install.
#
# Packages installed:
#   fastapi        — web framework for the proxy API
#   uvicorn        — ASGI server that runs the FastAPI app
#   litellm        — Bedrock client; normalizes to the OpenAI shape both ways
#   boto3          — AWS SDK + credential chain LiteLLM uses to reach Bedrock
#   orjson         — required by litellm's lazily-imported proxy http_parsing_utils
#   python-dotenv  — loads .env for local (non-Docker) development
#
# No torch, transformers, accelerate, or any GPU/ML libraries — all inference
# runs on Amazon Bedrock.
#
# constraints.txt LOCKS the full resolved set (incl. transitives). It is copied
# and passed with -c so a rebuild can't silently float a dependency: on
# 2026-07-16 an unpinned litellm range floated to 1.92.0 and took every Bedrock
# call down with "No module named 'orjson'" — with NO code change and a green
# /health. Regenerate constraints.txt only after verifying a REAL completion.
# -----------------------------------------------------------------------------
COPY requirements.txt constraints.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt -c constraints.txt


# -----------------------------------------------------------------------------
# Application source code
#
# Only src/ is needed at runtime. Project files (README, .env.example,
# .gitignore, etc.) are excluded via .dockerignore at project root.
# -----------------------------------------------------------------------------
COPY src/ ./src/


# -----------------------------------------------------------------------------
# Non-root user
#
# Create an unprivileged "appuser" and run the proxy as it. The container only
# runs a Python web app — it needs no root privileges, and dropping root limits
# the blast radius if the process is ever compromised. /app is chowned so the
# user can read the source it runs.
# -----------------------------------------------------------------------------
RUN useradd --create-home --shell /usr/sbin/nologin appuser && \
    chown -R appuser:appuser /app
USER appuser


# -----------------------------------------------------------------------------
# Healthcheck
#
# Hits the proxy's own /health endpoint (no auth required). Uses the stdlib
# urllib so no extra package (curl/wget) is added to the slim image. Exit 0 =>
# healthy, non-zero => unhealthy. /health returns 200 whenever the proxy is up,
# so this checks the proxy itself, not the (possibly cold) provider workers.
# -----------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8002/health', timeout=4).status == 200 else 1)"]


# -----------------------------------------------------------------------------
# Default command
#
# Runs the FastAPI proxy via uvicorn on port 8002. The only service this
# image runs. The proxy:
#   - Validates X-API-Key on every /chat and /embed request
#   - /chat  : maps reasoning_effort to Claude's thinking depth, routes to
#              BASE_MODEL (default) or NERVOUS_SYSTEM_MODEL based on the optional
#              model field, and streams the (OpenAI-shaped) SSE response back
#   - /embed : embeds the input via EMBEDDING_MODEL and returns the OpenAI-shaped
#              embeddings JSON (no streaming)
#   - Reports configuration + AWS-credential status for the three routes on
#     GET /health
# -----------------------------------------------------------------------------
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8002"]