"""careeragent-memory — the session-scoped retrieval layer for CareerAgent.

Two jobs, and only two:
  - POST /ingest    embed a turn (user input or agent output) and store its vector
  - POST /retrieve  embed the current query, rank this session's stored turns,
                    return the top-k most relevant

careeragent-memory RANKS. careeragent-api takes the retrieved turns and BUILDS the
final query. This service never assembles a prompt and never talks to /chat.

Embedding is delegated to careeragent-infra's /embed. Storage is memory's own
Postgres+pgvector (NOT the logger's shared DB). Auth is a single transport key
(MEMORY_API_KEY) inbound; no HMAC.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status

import security
from client.infra import InfraClient, InfraEmbedError
from retrieval import retrieve
from schemas import (
    EmbedHealth,
    HealthResponse,
    IngestRequest,
    IngestResponse,
    MemoryHealth,
    RetrieveRequest,
    RetrieveResponse,
)
from store import Store

VERSION = "0.1.0"

load_dotenv()


# --------------------------------------------------------------------------- #
# Configuration (read once at import; defaults documented in .env.example)
# --------------------------------------------------------------------------- #
def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required but is unset or empty.")
    return value


def _build_database_url() -> str:
    """Prefer MEMORY_DATABASE_URL; otherwise assemble from components."""
    url = os.getenv("MEMORY_DATABASE_URL", "").strip()
    if url:
        return url
    user = os.getenv("MEMORY_DB_USER", "careeragent_memory")
    password = _require("MEMORY_DB_PASSWORD")
    host = os.getenv("MEMORY_DB_HOST", "memory-db")
    port = os.getenv("MEMORY_DB_PORT", "5432")
    name = os.getenv("MEMORY_DB_NAME", "careeragent_memory")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


LOG_LEVEL = os.getenv("MEMORY_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("careeragent_memory.api")

def _bounded_top_k(raw: str) -> int:
    """Parse MEMORY_TOP_K_DEFAULT, clamped to the same 1..100 range the request
    schema enforces. The env default path bypasses Pydantic validation, so an
    out-of-range or non-numeric value (e.g. 0 -> 'LIMIT 0' returns nothing, or a
    negative value -> SQL error) would otherwise slip through unguarded."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("MEMORY_TOP_K_DEFAULT=%r is not an integer; using 5", raw)
        return 5
    if value < 1 or value > 100:
        clamped = min(100, max(1, value))
        logger.warning("MEMORY_TOP_K_DEFAULT=%d out of range 1..100; using %d", value, clamped)
        return clamped
    return value


TOP_K_DEFAULT = _bounded_top_k(os.getenv("MEMORY_TOP_K_DEFAULT", "5"))
MIN_SCORE = float(os.getenv("MEMORY_MIN_SCORE", "0.0"))
EMBED_TIMEOUT = float(os.getenv("MEMORY_EMBED_TIMEOUT", "10.0"))
EMBED_CONNECT_TIMEOUT = float(os.getenv("MEMORY_EMBED_CONNECT_TIMEOUT", "5.0"))


# --------------------------------------------------------------------------- #
# Shared clients (constructed in lifespan)
# --------------------------------------------------------------------------- #
infra_client: InfraClient | None = None
store: Store | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global infra_client, store

    # Configure inbound auth first — refuse to serve without BOTH the transport
    # key and the HMAC integrity secret (compartmentalized, like the logger).
    security.configure(_require("MEMORY_API_KEY"))
    security.configure_hmac(_require("MEMORY_HMAC_SECRET"))

    infra_client = InfraClient(
        base_url=_require("CAREERAGENT_INFRA_URL"),
        api_key=_require("INFRA_API_KEY"),
        connect_timeout=EMBED_CONNECT_TIMEOUT,
        read_timeout=EMBED_TIMEOUT,
    )
    store = Store(_build_database_url())

    await infra_client.start()
    await store.start()
    logger.info("careeragent-memory %s ready on :%s", VERSION, os.getenv("MEMORY_PORT", "8004"))

    try:
        yield
    finally:
        # Stop the store last so any in-flight work can settle first.
        if infra_client is not None:
            await infra_client.aclose()
        if store is not None:
            await store.aclose()


app = FastAPI(
    title="careeragent-memory",
    version=VERSION,
    description="Session-scoped retrieval layer for CareerAgent.",
    lifespan=lifespan,
    docs_url="/docs",
)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/")
async def root() -> dict:
    """Unauthenticated service identification (for platform health probes that
    cannot pre-share the API key)."""
    return {"service": "careeragent-memory", "version": VERSION}


@app.post("/ingest", response_model=IngestResponse, status_code=status.HTTP_201_CREATED)
async def ingest(req: IngestRequest, _: None = Depends(security.require_api_key)) -> IngestResponse:
    """Embed one turn and store its vector.

    Unlike retrieval, ingest does NOT fail open silently: a dropped ingest would
    invisibly mean the turn never enters the index and never surfaces in future
    retrieval. With a persistent store the write cleanly succeeds or errors, so
    we surface an embedding outage as 503 and let careeragent-api log/retry off the
    user's critical path.
    """
    assert infra_client is not None and store is not None  # set in lifespan

    # Verify the HMAC envelope before doing any work. The signed payload subset
    # is {role, content} — MUST match careeragent-api/src/client/memory.py.
    security.enforce(
        operation="ingest",
        request_id=req.request_id,
        client_timestamp=req.client_timestamp,
        source_service=req.source_service,
        session_id=req.session_id,
        payload={"role": req.role, "content": req.content},
        signature=req.hmac_signature,
    )

    try:
        vector = await infra_client.embed_one(req.content)
    except InfraEmbedError as exc:
        logger.warning("Ingest failed — embedding unavailable (session=%s): %s", req.session_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding unavailable; turn not stored",
        ) from exc

    try:
        result = await store.upsert_turn(req.session_id, req.role, req.content, vector)
    except Exception as exc:  # noqa: BLE001 - surface a clean 500 to the caller
        logger.error("Ingest failed — store error (session=%s): %s", req.session_id, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store turn",
        ) from exc

    return IngestResponse(
        session_id=req.session_id,
        role=req.role,
        stored=not result["duplicate"],
        duplicate=result["duplicate"],
        id=result["id"],
    )


@app.post("/retrieve", response_model=RetrieveResponse)
async def retrieve_endpoint(
    req: RetrieveRequest, _: None = Depends(security.require_api_key)
) -> RetrieveResponse:
    """Return the top-k stored turns most relevant to the query (this session).

    Always HTTP 200. If embedding is unavailable, returns an empty list with
    degraded=True so careeragent-api proceeds with recent turns only.
    """
    assert infra_client is not None and store is not None  # set in lifespan

    # Verify the HMAC envelope before doing any work. The signed payload subset
    # is {query} plus {top_k} when present — MUST match
    # careeragent-api/src/client/memory.py.
    retrieve_payload: dict = {"query": req.query}
    if req.top_k is not None:
        retrieve_payload["top_k"] = req.top_k
    security.enforce(
        operation="retrieve",
        request_id=req.request_id,
        client_timestamp=req.client_timestamp,
        source_service=req.source_service,
        session_id=req.session_id,
        payload=retrieve_payload,
        signature=req.hmac_signature,
    )

    top_k = req.top_k if req.top_k is not None else TOP_K_DEFAULT
    retrieved, degraded = await retrieve(
        infra=infra_client,
        store=store,
        session_id=req.session_id,
        query=req.query,
        top_k=top_k,
        min_score=MIN_SCORE,
    )
    return RetrieveResponse(session_id=req.session_id, retrieved=retrieved, degraded=degraded)


@app.get("/health", response_model=HealthResponse)
async def health(_: None = Depends(security.require_api_key)) -> HealthResponse:
    """Report memory's own readiness; embedding reachability is informational.

    status is `ok` whenever memory's store is reachable. Because memory fails
    open, an unreachable embedder degrades retrieval quality without taking the
    service down — so it does not flip status to unhealthy.
    """
    assert infra_client is not None and store is not None  # set in lifespan

    store_ok = await store.ping()
    embed_status, embed_url = await infra_client.embed_health()

    status_str = "ok" if store_ok else "unhealthy"
    return HealthResponse(
        status=status_str,
        careeragent_memory=MemoryHealth(
            version=VERSION,
            store="connected" if store_ok else "disconnected",
        ),
        careeragent_infra_embed=EmbedHealth(url=embed_url, status=embed_status),
    )
