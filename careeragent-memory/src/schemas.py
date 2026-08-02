"""Pydantic models for the careeragent-memory wire contract.

Two inbound endpoints:
  - POST /ingest    write one turn (the user's input, or the agent's output) into the store
  - POST /retrieve  rank a session's stored turns against the current query

careeragent-memory RANKS. It does not build the prompt — careeragent-api takes the
retrieved turns and assembles the final query. These models reflect that split:
/retrieve returns candidates with scores, never an assembled prompt.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Role = Literal["user", "assistant"]


class SignedEnvelope(BaseModel):
    """Fields every signed request carries so memory can verify HMAC integrity
    and replay-freshness. The caller (careeragent-api) signs an envelope built
    from these plus the operation-specific payload; see src/security.py.
    """

    request_id: str = Field(..., min_length=1, max_length=128)
    client_timestamp: datetime
    source_service: str = Field(..., min_length=1, max_length=64)
    hmac_signature: str = Field(..., min_length=64, max_length=64)


class IngestRequest(SignedEnvelope):
    """A single turn to embed and store.

    Called twice per /chat turn by careeragent-api: once with the user input and
    once with the assistant output. Both pieces of text are already computed by
    careeragent-api for the logger's conversation_capture, so ingest reuses them.

    The signed payload subset is {role, content}; the envelope fields above are
    signed alongside it (see security.compute_signature).
    """

    session_id: str = Field(..., min_length=1, max_length=128)
    role: Role
    content: str = Field(..., min_length=1)


class IngestResponse(BaseModel):
    session_id: str
    role: Role
    stored: bool
    duplicate: bool = False
    id: Optional[str] = None


class RetrieveRequest(SignedEnvelope):
    """Ask for the top-k stored turns most relevant to `query`, within a session.

    The signed payload subset is {query} plus {top_k} when top_k is not None;
    the envelope fields are signed alongside it (see security.compute_signature).
    """

    session_id: str = Field(..., min_length=1, max_length=128)
    query: str = Field(..., min_length=1)
    top_k: Optional[int] = Field(default=None, ge=1, le=100)


class RetrievedTurn(BaseModel):
    id: str
    role: Role
    content: str
    score: float
    created_at: Optional[str] = None


class RetrieveResponse(BaseModel):
    session_id: str
    retrieved: List[RetrievedTurn]
    # True when retrieval failed open (embedding unavailable/cold/unconfigured).
    # careeragent-api reads this to decide whether to proceed with recent turns only.
    degraded: bool = False


class EmbedHealth(BaseModel):
    url: str
    status: str


class MemoryHealth(BaseModel):
    version: str
    store: str  # "connected" | "disconnected"


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded" | "unhealthy"
    careeragent_memory: MemoryHealth
    careeragent_infra_embed: EmbedHealth
