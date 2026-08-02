"""
src/api/main.py

CareerAgent inference API — thin FastAPI proxy layer (Amazon Bedrock backend).

This file is the public-facing API for careeragent-infra. It sits between its
server-side callers (careeragent-api today, and potentially other internal
services as the system grows) and Amazon Bedrock, handling authentication and
request validation before forwarding. For chat it streams the model's response
back as Server-Sent Events; for embeddings it returns a single JSON response.

Every path to a model in the CareerAgent system goes through this proxy — chat
models and the embedding model alike. Nothing talks to Bedrock directly; this
service is the single chokepoint that holds the AWS credentials and presents a
stable, OpenAI-shaped contract to its callers.

Provider: Amazon Bedrock via LiteLLM
---------------------------------------
The proxy speaks to Bedrock through LiteLLM, which normalizes Bedrock's
request/response shapes to the OpenAI format on BOTH ends. That is what lets
this service swap the backend from an OpenAI-compatible HTTP endpoint to
Bedrock without changing a single byte of the contract the rest of the stack
depends on:

  - /chat  : LiteLLM yields OpenAI-shaped ChatCompletion chunks; the proxy
             re-emits them as `data: {json}\\n\\n` SSE events, ending with
             `data: [DONE]\\n\\n`. Claude's extended-thinking tokens (LiteLLM
             surfaces them as `delta.reasoning_content`) are re-mapped onto the
             contract's `delta.reasoning` field, then visible answer tokens
             arrive in `delta.content`.
  - /embed : LiteLLM returns an OpenAI-shaped embeddings response; the proxy
             returns it as JSON unchanged in shape.

Because the wire format the callers see is identical to the previous
OpenAI-compatible backend, careeragent-api, careeragent-frontend, and
careeragent-memory require NO changes.

Authentication
---------------------------------------
  - Inbound : every /chat and /embed request must carry a valid X-API-Key
              header (validated against API_KEY). Same contract as before.
  - Outbound: Bedrock is reached with AWS credentials resolved from the
              standard chain (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
              AWS_SESSION_TOKEN env vars, a shared profile, or an attached IAM
              role) plus AWS_REGION_NAME. There is no provider Bearer key.

Model routing
---------------------------------------
A /chat request selects a Bedrock model id by its `model` field:
  - model="base" (default)  → BASE_MODEL            (the coach reasoning model)
  - model="nervous_system"  → NERVOUS_SYSTEM_MODEL  (fast control model)
  - POST /embed             → EMBEDDING_MODEL        (the embedding model)
Each is a LiteLLM Bedrock model id, e.g.
  bedrock/us.anthropic.claude-opus-4-8
  bedrock/us.anthropic.claude-haiku-4-5
  bedrock/amazon.titan-embed-text-v2:0

Reasoning effort
---------------------------------------
The optional `reasoning_effort` field (low | medium | high, default medium) is
passed to LiteLLM as `reasoning_effort`, which maps it to Claude's
extended-thinking depth on Bedrock. (The previous backend read it from an
injected "Reasoning: <level>" system line; that gpt-oss convention is gone —
the level now drives real Anthropic thinking.) The embedding model does not
reason.

Endpoints
---------------------------------------
POST /chat     → text/event-stream (SSE), OpenAI ChatCompletion chunks, [DONE]
POST /complete → application/json, OpenAI completion incl. tool_calls (non-streaming)
POST /embed    → application/json, OpenAI-compatible embeddings response
GET  /health   → {"status": "ok" | "degraded", ...} (no auth)

Tool transport (/complete)
---------------------------------------
/complete is the tool-aware, non-streaming call the agent loop (careeragent-api)
uses. It forwards `messages` + optional `tools`/`tool_choice` to the model and
returns the OpenAI completion whose `choices[0].message` carries `content` and,
when the model decides to act, `tool_calls`. This proxy only TRANSPORTS tools and
tool_calls — it never defines, executes, or loops on them; that orchestration is
the agent's job. (Same boundary spirit as passing `reasoning_effort` through.)

Usage
---------------------------------------
Start via docker-compose:
    docker-compose up careeragent-infra

Or directly with uvicorn for local dev:
    uvicorn src.api.main:app --host 0.0.0.0 --port 8002 --reload
"""

import asyncio
import json
import logging
import os
import random
import secrets
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator, List, Literal, Optional, Union

# Use LiteLLM's BUNDLED model-cost map instead of fetching it from GitHub on
# import. The remote fetch phones raw.githubusercontent.com at startup and times
# out when the network is offline/slow, adding launch latency and a scary warning
# to a containerized service that should never need GitHub to boot. MUST be set
# BEFORE `import litellm`. (Lesson ported from openagent-code's Bedrock work.)
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

# Load .env for local (non-Docker) development. Under docker-compose the
# values arrive via env_file, so this is a no-op there.
load_dotenv()

# LiteLLM configuration:
#   drop_params        — silently drop request params a model doesn't support
#                        (e.g. reasoning_effort on a non-thinking model) instead
#                        of erroring. Keeps the proxy robust across model choices.
#   modify_params      — let LiteLLM reshape the message list to each provider's
#                        rules. Bedrock's Converse API requires strict
#                        user<->assistant alternation and maps tool-results to
#                        user-side blocks; a turn with a consecutive same-role run
#                        is otherwise REJECTED. With this on, LiteLLM inserts the
#                        needed continuation messages instead of 400-ing.
#   suppress_debug_info — quiet LiteLLM's "Give Feedback / Get Help" banner on
#                        every error/retry, which clutters our own retry logs.
litellm.drop_params = True
litellm.modify_params = True
litellm.suppress_debug_info = True

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("careeragent-infra")


# ---------------------------------------------------------------------------
# Environment configuration
#
# API_KEY              : Secret validated against the X-API-Key header on /chat
#                        and /embed. The same value careeragent-api holds as
#                        INFRA_API_KEY. Isolated in verify_api_key for a future
#                        per-caller swap.
# AWS_REGION_NAME      : AWS region for Bedrock (e.g. us-east-1). Required.
#                        Falls back to AWS_REGION if AWS_REGION_NAME is unset.
# AWS_ACCESS_KEY_ID /  : AWS credentials. Read directly by LiteLLM/boto3 from
# AWS_SECRET_ACCESS_KEY  the environment (or a profile / IAM role) — this module
# AWS_SESSION_TOKEN      does NOT read them, it only checks they resolve for
#                        /health. Never logged.
# BASE_MODEL           : LiteLLM Bedrock model id for the base (coach) model.
#                        Default route for all /chat requests. Required.
#                        e.g. bedrock/us.anthropic.claude-opus-4-8
# NERVOUS_SYSTEM_MODEL : LiteLLM Bedrock model id for the fast control model.
#                        Used when model="nervous_system". Optional — when
#                        unset, that route is "not configured".
# EMBEDDING_MODEL      : LiteLLM Bedrock model id for the embedding model.
#                        Used by POST /embed. Optional — when unset, /embed
#                        returns "not configured" and /chat is unaffected.
#                        e.g. bedrock/amazon.titan-embed-text-v2:0
# REASONING_EFFORT     : Default reasoning effort if not set per request.
#                        Applies to both chat models. low | medium (default) | high.
# MAX_TOKENS           : Max output tokens for /chat generations. Bedrock's
#                        Anthropic models require this; LiteLLM forwards it.
#                        Must exceed the thinking budget at high effort. Default 8192.
# REQUEST_TIMEOUT      : Per-request timeout (seconds) for Bedrock calls. Bedrock
#                        is always-warm (no scale-to-zero), so this is a real
#                        bound, not a cold-start absorber. Default 600.
# ---------------------------------------------------------------------------
API_KEY               = os.environ.get("API_KEY", "")
AWS_REGION_NAME       = os.environ.get("AWS_REGION_NAME", "") or os.environ.get("AWS_REGION", "")
BASE_MODEL            = os.environ.get("BASE_MODEL", "")
NERVOUS_SYSTEM_MODEL  = os.environ.get("NERVOUS_SYSTEM_MODEL", "")
EMBEDDING_MODEL       = os.environ.get("EMBEDDING_MODEL", "")
REASONING_EFFORT      = os.environ.get("REASONING_EFFORT", "medium")

try:
    MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "8192"))
except ValueError:
    MAX_TOKENS = 8192

try:
    REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "600"))
except ValueError:
    REQUEST_TIMEOUT = 600.0

# MODEL_RETRIES / BACKOFF_CAP — Bedrock throws bursts of transient 503s
# (ServiceUnavailableError / throttling) on large requests; a single attempt
# surfaces them straight to the caller. Retry transient errors with exponential
# backoff + jitter, capped at BACKOFF_CAP seconds. (Tuned per openagent-code's
# Bedrock migration: a flat low cap gave up before the 503 burst cleared.)
try:
    MODEL_RETRIES = int(os.environ.get("MODEL_RETRIES", "5"))
except ValueError:
    MODEL_RETRIES = 5

try:
    BACKOFF_CAP = float(os.environ.get("BACKOFF_CAP", "20"))
except ValueError:
    BACKOFF_CAP = 20.0

# INFRA_ENABLE_DOCS : When "true", expose the FastAPI docs (/docs, /redoc) and
#                     the OpenAPI schema. Disabled by default — internal,
#                     server-to-server service. Set to "true" for local dev.
ENABLE_DOCS = os.environ.get("INFRA_ENABLE_DOCS", "").strip().lower() == "true"


# ---------------------------------------------------------------------------
# Model routing
#
# A /chat request selects a Bedrock model id by its `model` field. Resolving it
# up front (before the StreamingResponse starts) lets the endpoint return a real
# 503 when the selected route is unconfigured, instead of an opaque 200 stream
# carrying an [ERROR] event — mirroring how /embed guards EMBEDDING_MODEL.
# ---------------------------------------------------------------------------
def resolve_chat_model(model: Literal["base", "nervous_system"]) -> str:
    """Return the configured Bedrock model id for the selected chat route.

    Raises ValueError on an unexpected model value (defensive — the endpoint
    already constrains it to {"base", "nervous_system"}).
    """
    if model == "base":
        return BASE_MODEL
    if model == "nervous_system":
        return NERVOUS_SYSTEM_MODEL
    raise ValueError(f"unexpected model: {model!r}")


# ---------------------------------------------------------------------------
# AWS credential presence (for /health only — local, no network call)
# ---------------------------------------------------------------------------
def _aws_credentials_present() -> bool:
    """Return True if credentials for Bedrock resolve.

    A local check — a Bedrock bearer token or static keys (fast path), then a
    botocore session lookup (covers shared profiles and attached IAM roles).
    Never makes a Bedrock call and never logs the credentials.
    """
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        return True
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return True
    try:
        import boto3  # local import: only needed by the health path

        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Retry / backoff for transient Bedrock errors
#
# Bedrock returns bursts of transient 503s (ServiceUnavailableError) and 429
# throttles on large requests. A single attempt would surface those straight to
# the caller. We retry transient errors with exponential backoff + jitter, but
# fail fast on permanent ones (a 400 / access-denied / context overflow just
# fails again). Ported from openagent-code's Bedrock migration.
# ---------------------------------------------------------------------------
def _non_retryable(exc: Exception) -> bool:
    """True for errors retrying can't fix — a 400 BadRequest, an invalid request,
    an access-denied, a context-window overflow, or a BROKEN DEPLOYMENT. Re-sending
    the identical request only fails again, so we stop instead of burning every retry.

    The deployment case is not hypothetical: on 2026-07-16 litellm floated to a
    version whose completion() lazily imports a module we didn't ship (orjson).
    litellm wrapped the ImportError as APIConnectionError — which reads as
    'transient' — so every call burned 6 infra retries under 3 api retries = 28
    doomed attempts, turning an instant, permanent failure into a 3.7-minute hang.
    A missing module is never fixed by trying again: fail fast and surface it."""
    # Walk the cause chain — litellm wraps the real error in a connection error.
    seen: List[str] = []
    cur: Optional[BaseException] = exc
    for _ in range(6):                      # bounded; cause chains are short
        if cur is None:
            break
        seen.append(type(cur).__name__.lower())
        seen.append(str(cur).lower())
        cur = cur.__cause__ or cur.__context__
    blob = " ".join(seen)

    # A broken deployment (missing/incompatible dependency) — permanent.
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return True
    if "modulenotfounderror" in blob or "no module named" in blob:
        return True

    name = type(exc).__name__.lower()
    if "badrequest" in name or "contextwindow" in name or "invalidrequest" in name:
        return True
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "context length", "maximum context", "context window",
            "input is too long", "too many tokens", "exceeds the maximum",
            "validationexception", "accessdenied",
        )
    )


async def _backoff(attempt: int, why: str) -> None:
    """Exponential backoff with jitter, capped at BACKOFF_CAP. The jitter de-syncs
    retries; the cap matters for Bedrock 503 bursts, which a flat low cap gives up
    on before they clear. Async sleep so the event loop is not blocked."""
    delay = min(2 ** attempt, BACKOFF_CAP) + random.uniform(0, 1)
    logger.warning(
        "Retry: %s (attempt %d/%d) — waiting %.1fs", why, attempt + 1, MODEL_RETRIES, delay
    )
    await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# API key authentication  (unchanged contract)
# ---------------------------------------------------------------------------
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: str = Security(api_key_header)) -> str:
    """
    FastAPI dependency that validates the X-API-Key header.

    Raises 401 if the header is missing or the key does not match API_KEY.
    Returns the key on success. Shared by /chat and /embed.
    """
    # Constant-time comparison so a timing side-channel cannot recover the key.
    # compare_digest also guards the empty case.
    if not key or not secrets.compare_digest(key, API_KEY):
        logger.warning("Unauthorized request — invalid or missing API key")
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return key


# ---------------------------------------------------------------------------
# Lifespan — startup and shutdown logging / config validation
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.

    The proxy has no model to load — Bedrock owns that. Lifespan is used for
    config validation and logging only.
    """
    global REASONING_EFFORT

    # Validate the default reasoning effort. Coerce to medium on a bad value.
    if REASONING_EFFORT not in ("low", "medium", "high"):
        logger.warning(
            "REASONING_EFFORT=%r is not one of low|medium|high — coercing to 'medium'",
            REASONING_EFFORT,
        )
        REASONING_EFFORT = "medium"

    creds_ok = _aws_credentials_present()

    logger.info("=== CareerAgent Inference API Starting (Bedrock backend) ===")
    logger.info("Proxy port            : 8002")
    logger.info("AWS region            : %s", AWS_REGION_NAME or "NOT SET")
    logger.info("AWS credentials       : %s", "resolved" if creds_ok else "NOT RESOLVED")
    logger.info("Base model            : %s", BASE_MODEL or "NOT SET")
    logger.info("Nervous-system model  : %s", NERVOUS_SYSTEM_MODEL or "(not configured)")
    logger.info("Embedding model       : %s", EMBEDDING_MODEL or "(not configured)")
    logger.info("Default reasoning     : %s", REASONING_EFFORT)
    logger.info("Max output tokens     : %d", MAX_TOKENS)
    logger.info("Request timeout (s)   : %s", REQUEST_TIMEOUT)
    logger.info("Model retries         : %d (backoff cap %.0fs)", MODEL_RETRIES, BACKOFF_CAP)
    logger.info("API docs (/docs)      : %s", "enabled" if ENABLE_DOCS else "disabled")
    if not AWS_REGION_NAME:
        logger.warning("AWS_REGION_NAME is not set — Bedrock calls will fail until it is configured")
    if not creds_ok:
        logger.warning("AWS credentials did not resolve — Bedrock calls will fail until they are configured")
    # Exercise the REAL completion path in the background (see _probe_loop). Config
    # being valid proved nothing on 2026-07-16 — a lazily-imported missing module
    # took every call down while /health stayed green. This is what catches that.
    probe_task = asyncio.create_task(_probe_loop())

    logger.info("=== CareerAgent Inference API Ready — listening on :8002 ===")

    yield

    probe_task.cancel()
    try:
        await probe_task
    except asyncio.CancelledError:
        pass

    logger.info("=== CareerAgent Inference API Shutting Down ===")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="CareerAgent Inference API",
    description="Model inference proxy (Amazon Bedrock) — the model serving layer of the CareerAgent system.",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)


# ---------------------------------------------------------------------------
# Catch-all exception handler
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ---------------------------------------------------------------------------
# Request schemas  (unchanged contract)
# ---------------------------------------------------------------------------
class Message(BaseModel):
    """Single message in the OpenAI messages format."""
    role: str
    content: str


class ChatRequest(BaseModel):
    """
    Request body for POST /chat.

    messages         : Full OpenAI messages list. The caller (careeragent-api)
                       includes the persona as the first system message. This
                       proxy forwards the list to Bedrock unmodified.
    reasoning_effort : low | medium | high (optional; default REASONING_EFFORT).
                       Maps to Claude's extended-thinking depth.
    model            : "base" (default) or "nervous_system".
    """
    messages:         List[Message]
    reasoning_effort: Optional[Literal["low", "medium", "high"]] = None
    model:            Optional[Literal["base", "nervous_system"]] = None


class EmbedRequest(BaseModel):
    """
    Request body for POST /embed.

    input : A single string or a list of strings. A list is embedded in one
            batched call.
    """
    input: Union[str, List[str]]


class CompleteRequest(BaseModel):
    """
    Request body for POST /complete — the tool-aware, NON-streaming completion
    the agent loop calls.

    messages         : Full OpenAI messages list. Passed through as RAW dicts (not
                       the strict Message model) so assistant `tool_calls` and
                       `tool`-role result turns survive verbatim — infra transports
                       the conversation, it does not reshape it.
    tools            : OpenAI function-tool schemas, forwarded to the model.
    tool_choice      : "auto" | "none" | "required" | {...}; forwarded as-is.
    reasoning_effort : low | medium | high (optional; default REASONING_EFFORT).
    model            : "base" (default) or "nervous_system".

    This proxy TRANSPORTS tools/tool_calls; it never defines, executes, or loops
    on them — that is careeragent-api (the agent).
    """
    messages:         List[dict]
    tools:            Optional[List[dict]] = None
    tool_choice:      Optional[Union[str, dict]] = None
    reasoning_effort: Optional[Literal["low", "medium", "high"]] = None
    model:            Optional[Literal["base", "nervous_system"]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def to_message_dicts(messages: List[Message]) -> List[dict]:
    """Convert pydantic Message objects to the plain dicts LiteLLM expects."""
    return [{"role": m.role, "content": m.content} for m in messages]


def _sse_chunk(chunk) -> Optional[str]:
    """
    Translate one LiteLLM streaming chunk into a contract-shaped SSE event.

    We build the OpenAI ChatCompletion-chunk dict by hand rather than dumping
    LiteLLM's object verbatim, so the wire format matches the careeragent-infra
    contract EXACTLY regardless of LiteLLM internals — in particular, Claude's
    thinking tokens (LiteLLM's `delta.reasoning_content`) are emitted on the
    contract's `delta.reasoning` field. Returns None for an empty chunk.
    """
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return None

    out_choices = []
    for c in choices:
        delta_obj = getattr(c, "delta", None)
        delta: dict = {}
        if delta_obj is not None:
            role = getattr(delta_obj, "role", None)
            content = getattr(delta_obj, "content", None)
            reasoning = getattr(delta_obj, "reasoning_content", None)
            if role:
                delta["role"] = role
            if content:
                delta["content"] = content
            if reasoning:
                delta["reasoning"] = reasoning
        out_choices.append(
            {
                "index": getattr(c, "index", 0),
                "delta": delta,
                "finish_reason": getattr(c, "finish_reason", None),
            }
        )

    payload = {
        "id": getattr(chunk, "id", "") or "",
        "object": "chat.completion.chunk",
        "created": getattr(chunk, "created", 0) or 0,
        "model": getattr(chunk, "model", "") or "",
        "choices": out_choices,
    }
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# SSE proxy stream (for /chat)
#
# Calls Bedrock via LiteLLM with streaming and re-emits each normalized chunk as
# an SSE event, ending with [DONE]. Provider failures surface as an in-stream
# [ERROR] event (the response is already HTTP 200 once the stream begins).
# ---------------------------------------------------------------------------
async def proxy_stream(
    messages: List[Message],
    reasoning_effort: str,
    model: Literal["base", "nervous_system"] = "base",
) -> AsyncGenerator[str, None]:
    """
    Async generator that forwards the chat request to the selected Bedrock model
    and yields contract-shaped SSE chunks back to the caller as they arrive.
    """
    model_id = resolve_chat_model(model)
    started = False  # have we yielded any chunk to the caller yet?

    # Retry transient failures, but ONLY before the first chunk is on the wire —
    # once the caller has received bytes the 200 stream is committed and cannot be
    # restarted. A mid-stream failure (or a permanent error, or the last attempt)
    # surfaces as a generic in-stream [ERROR].
    for attempt in range(MODEL_RETRIES + 1):
        last = attempt == MODEL_RETRIES
        try:
            response = await litellm.acompletion(
                model=model_id,
                messages=to_message_dicts(messages),
                stream=True,
                reasoning_effort=reasoning_effort,  # → Claude extended-thinking depth
                max_tokens=MAX_TOKENS,
                aws_region_name=AWS_REGION_NAME,
                timeout=REQUEST_TIMEOUT,
            )

            async for chunk in response:
                event = _sse_chunk(chunk)
                if event:
                    started = True
                    yield event

            yield "data: [DONE]\n\n"
            return

        except Exception as exc:
            # Log server-side detail, but never put the raw exception text into the
            # caller-facing stream — it can carry model ids / account detail.
            logger.error("Bedrock chat error (model=%s): %s", model, exc)
            if started or last or _non_retryable(exc):
                yield "data: [ERROR] internal proxy error\n\n"
                yield "data: [DONE]\n\n"
                return
            await _backoff(attempt, type(exc).__name__)


# ---------------------------------------------------------------------------
# Embedding proxy (for /embed) — non-streaming
# ---------------------------------------------------------------------------
async def proxy_embed(inputs: Union[str, List[str]]) -> dict:
    """
    Embed the input(s) via Bedrock through LiteLLM and return the OpenAI-shaped
    embeddings response as a plain dict ({"object":"list","data":[...],...}).
    Raises on provider error; the /embed endpoint maps that to 502/503.
    """
    # Retry transient Bedrock errors (throttle / 503 burst); fail fast on a
    # permanent error. /embed is non-streaming, so a full retry is safe.
    for attempt in range(MODEL_RETRIES + 1):
        last = attempt == MODEL_RETRIES
        try:
            response = await litellm.aembedding(
                model=EMBEDDING_MODEL,
                input=inputs,
                aws_region_name=AWS_REGION_NAME,
                timeout=REQUEST_TIMEOUT,
            )

            # LiteLLM's EmbeddingResponse is OpenAI-shaped; model_dump() yields the
            # contract JSON. Fall back to dict() on older LiteLLM versions.
            try:
                payload = response.model_dump()
            except AttributeError:
                payload = dict(response)

            # Guarantee the top-level shape callers rely on.
            payload.setdefault("object", "list")
            return payload

        except Exception as exc:
            if last or _non_retryable(exc):
                raise
            await _backoff(attempt, type(exc).__name__)


# ---------------------------------------------------------------------------
# Tool-aware completion proxy (for /complete) — non-streaming
#
# The agent loop needs the COMPLETE tool_calls array before it can act (you
# can't execute half a tool call), so this call is non-streaming. Only the
# agent's final answer streams to the user (via /chat), not these inner turns.
# Transport only: tools go to the model, tool_calls come back — infra does not
# execute or loop.
# ---------------------------------------------------------------------------
async def proxy_complete(
    messages: List[dict],
    reasoning_effort: str,
    model: Literal["base", "nervous_system"] = "base",
    tools: Optional[List[dict]] = None,
    tool_choice: Optional[Union[str, dict]] = None,
) -> dict:
    """
    Forward a tool-aware request to the selected Bedrock model and return the
    OpenAI-shaped completion as a plain dict (choices[0].message carries content
    and, when the model acts, tool_calls). Retries transient Bedrock errors;
    fails fast (raises) on a permanent error. Non-streaming, so a full retry is
    safe.
    """
    model_id = resolve_chat_model(model)
    kwargs = dict(
        model=model_id,
        messages=messages,               # raw dicts, forwarded verbatim
        stream=False,
        reasoning_effort=reasoning_effort,
        max_tokens=MAX_TOKENS,
        aws_region_name=AWS_REGION_NAME,
        timeout=REQUEST_TIMEOUT,
    )
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"

    for attempt in range(MODEL_RETRIES + 1):
        last = attempt == MODEL_RETRIES
        try:
            response = await litellm.acompletion(**kwargs)
            # LiteLLM's ModelResponse is OpenAI-shaped; model_dump() yields the
            # contract JSON (choices[].message.tool_calls[].function.arguments).
            try:
                return response.model_dump()
            except AttributeError:
                return dict(response)
        except Exception as exc:
            logger.error("Bedrock complete error (model=%s): %s", model, exc)
            if last or _non_retryable(exc):
                raise
            await _backoff(attempt, type(exc).__name__)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/chat")
async def chat(
    request: ChatRequest,
    api_key: str = Security(verify_api_key),
):
    """
    Inference endpoint — OpenAI messages format, SSE streaming, Bedrock backend.

    Validates the API key and request body, then forwards to the selected
    Bedrock model and streams the response back as Server-Sent Events. Requires
    a valid X-API-Key header (401 otherwise).
    """
    if not request.messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty")

    roles = [m.role for m in request.messages]
    if "user" not in roles:
        raise HTTPException(
            status_code=400,
            detail="Messages must include at least one user message",
        )

    effort     = request.reasoning_effort or REASONING_EFFORT
    model_name = request.model or "base"

    # Pre-flight: fail with a real 503 if the selected route is unconfigured,
    # before the 200 stream begins (mirrors how /embed guards EMBEDDING_MODEL).
    if not resolve_chat_model(model_name):
        logger.warning("POST /chat for model=%s but its model id is not configured", model_name)
        raise HTTPException(status_code=503, detail=f"{model_name} model is not configured")

    logger.info(
        "POST /chat | messages: %d | reasoning: %s | model: %s",
        len(request.messages),
        effort,
        model_name,
    )

    return StreamingResponse(
        proxy_stream(request.messages, effort, model_name),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/complete")
async def complete(
    request: CompleteRequest,
    api_key: str = Security(verify_api_key),
):
    """
    Tool-aware, NON-streaming completion — the call the agent loop makes.

    Forwards messages (+ optional tools/tool_choice) to the selected Bedrock
    model and returns the OpenAI-shaped completion JSON. When the model decides
    to act, `choices[0].message.tool_calls` carries the calls; careeragent-api
    executes them and loops. This proxy only transports.

    Responses
    ---------
    200 : OpenAI-shaped completion JSON (content and/or tool_calls)
    400 : messages list empty
    401 : missing or invalid X-API-Key
    502 : the model provider returned an error
    503 : selected chat model not configured
    """
    if not request.messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty")

    effort     = request.reasoning_effort or REASONING_EFFORT
    model_name = request.model or "base"

    # Pre-flight: fail with a real 503 if the selected route is unconfigured.
    if not resolve_chat_model(model_name):
        logger.warning("POST /complete for model=%s but its model id is not configured", model_name)
        raise HTTPException(status_code=503, detail=f"{model_name} model is not configured")

    logger.info(
        "POST /complete | messages: %d | tools: %d | reasoning: %s | model: %s",
        len(request.messages),
        len(request.tools or []),
        effort,
        model_name,
    )

    try:
        payload = await proxy_complete(
            request.messages, effort, model_name, request.tools, request.tool_choice,
        )
    except Exception as exc:
        # Log server-side detail; return a generic message (the raw exception
        # can carry the model id / account detail).
        logger.error("Complete error: %s", exc)
        raise HTTPException(status_code=502, detail="Model provider error")

    return JSONResponse(status_code=200, content=payload)


@app.post("/embed")
async def embed(
    request: EmbedRequest,
    api_key: str = Security(verify_api_key),
):
    """
    Embedding endpoint — OpenAI embeddings format, single JSON response, Bedrock.

    Responses
    ---------
    200 : OpenAI-compatible embeddings JSON
    400 : input is empty
    401 : missing or invalid X-API-Key
    502 : the embedding provider returned an error, or an unexpected proxy error
    503 : embedding model not configured (EMBEDDING_MODEL unset)
    """
    # Validate that input is present and non-empty.
    if isinstance(request.input, str):
        if not request.input.strip():
            raise HTTPException(status_code=400, detail="Input cannot be empty")
        input_count = 1
    else:
        if len(request.input) == 0:
            raise HTTPException(status_code=400, detail="Input list cannot be empty")
        if any(not item.strip() for item in request.input):
            raise HTTPException(
                status_code=400,
                detail="Input list cannot contain empty strings",
            )
        input_count = len(request.input)

    # Graceful "not configured" — the embedding route is optional, like the
    # nervous-system route. /chat is entirely unaffected when this is unset.
    if not EMBEDDING_MODEL:
        logger.warning("POST /embed called but EMBEDDING_MODEL is not set")
        raise HTTPException(status_code=503, detail="Embedding model not configured")

    logger.info("POST /embed | inputs: %d", input_count)

    try:
        payload = await proxy_embed(request.input)
    except Exception as exc:
        # Log server-side detail; return a generic message (the raw exception
        # can carry the model id / account detail).
        logger.error("Embedding error: %s", exc)
        raise HTTPException(status_code=502, detail="Embedding provider error")

    return JSONResponse(status_code=200, content=payload)


# ---------------------------------------------------------------------------
# Model-path prober
#
# WHY: /health used to check only that a model id was set and AWS credentials
# resolved — it never exercised the model path. On 2026-07-16 litellm floated to
# a version whose acompletion() LAZILY imports a module we didn't ship (orjson).
# Config and credentials were perfect, so /health served 673 requests, all 200,
# and docker reported "healthy" — through an outage in which 8/8 completions
# failed. A readiness probe that never runs the real code path cannot detect a
# broken deployment.
#
# So: a BACKGROUND task exercises a real (tiny) completion on the same
# proxy_complete path the agent uses, and /health reports its last result. The
# probe is deliberately NOT awaited inside /health — a slow Bedrock call must
# never time out the healthcheck and restart-loop the container.
# ---------------------------------------------------------------------------
_PROBE_OK_EVERY = 300.0     # re-verify a healthy model path every 5 min (~trivial cost)
_PROBE_FAIL_EVERY = 30.0    # a failing path re-checks sooner so it can self-heal
_model_path: dict = {"ok": None, "error": None}


async def _probe_once() -> None:
    """Run one real completion through the agent's own path and record the result."""
    try:
        await proxy_complete(messages=[{"role": "user", "content": "ping"}],
                             reasoning_effort="low")
        if _model_path.get("ok") is not True:
            logger.info("model-path probe OK — a real completion succeeded")
        _model_path.update(ok=True, error=None)
    except Exception as exc:  # noqa: BLE001 — the probe must never raise
        detail = f"{type(exc).__name__}: {exc}"[:300]
        if _model_path.get("error") != detail:
            logger.error("model-path probe FAILED — completions are broken: %s", detail)
        _model_path.update(ok=False, error=detail)


async def _probe_loop() -> None:
    """Probe at startup, then on a cadence. Cancelled at shutdown."""
    while True:
        await _probe_once()
        await asyncio.sleep(_PROBE_OK_EVERY if _model_path.get("ok") else _PROBE_FAIL_EVERY)


@app.get("/health")
async def health():
    """
    Health check endpoint. No authentication required.

    With the Bedrock backend, "reachable" means *configured and credentialed*:
    Bedrock is a managed, always-on endpoint, so there is no host to ping and no
    cold/scale-to-zero worker to distinguish. A route is "ok" when its model id
    is set AND AWS credentials + region resolve; "unreachable" when configured
    but credentials/region are missing; "not configured" when its model id is
    unset. Actual per-model access errors surface at /chat or /embed call time.

    Top-level `status` is "ok" when the base route is ok, "degraded" otherwise.
    The shape is identical to the previous backend's /health response.
    """
    creds_ok = bool(AWS_REGION_NAME) and _aws_credentials_present()

    def route_status(model_id: str) -> str:
        if not model_id:
            return "not configured"
        return "ok" if creds_ok else "unreachable"

    # Config+credentials are necessary but NOT sufficient — the model PATH must
    # actually work. `probe_ok is None` means the first probe hasn't landed yet;
    # treat that as not-yet-failing so a booting container isn't killed.
    probe_ok = _model_path.get("ok")
    path_broken = probe_ok is False
    base_model_ok = bool(BASE_MODEL) and creds_ok and not path_broken

    payload = {
        "status":         "ok" if base_model_ok else "degraded",
        "proxy":          "ok",
        "base_model":     "ok" if base_model_ok else "unreachable",
        "nervous_system": route_status(NERVOUS_SYSTEM_MODEL),
        "embedding":      route_status(EMBEDDING_MODEL),
        # The real signal: did an actual completion succeed recently?
        "model_path":     "ok" if probe_ok else ("failing" if path_broken else "pending"),
    }
    if path_broken:
        payload["model_path_error"] = _model_path.get("error")
    return payload
