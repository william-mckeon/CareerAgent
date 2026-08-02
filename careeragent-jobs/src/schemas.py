"""
src/schemas.py

Request/response models for careeragent-jobs.

The request shape mirrors what careeragent-api's JobsClient sends: a job kind, an
opaque spec the handler interprets, and the conversation the result should be
injected into when the job finishes.
"""
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class JobCreate(BaseModel):
    """Body for POST /jobs.

    kind            : the job kind (only ``review_repos`` is valid in this slice).
    spec            : opaque per-kind parameters (e.g. {repos, limit, focus, force}).
    conversation_id : optional UUID. When set, the finished result is injected as
                      an assistant message into that conversation.
    """
    kind: str
    spec: Dict[str, Any] = Field(default_factory=dict)
    conversation_id: Optional[str] = None


class JobCreated(BaseModel):
    """201 response for POST /jobs."""
    id: str
    status: str


class JobOut(BaseModel):
    """Public job view for GET /jobs/{id} and GET /jobs (spec is intentionally
    NOT exposed — it is worker-internal)."""
    id: str
    kind: str
    status: str
    attempts: int
    result: Optional[str] = None
    error: Optional[str] = None
    conversation_id: Optional[str] = None
    created_at: str
    updated_at: str


class ScheduleOut(BaseModel):
    """Public view of a recurring schedule for GET /schedules (P7 #18b). Read-only
    observability into what the scheduler will fire and when; spec is omitted for
    the same reason JobOut omits it (worker-internal)."""
    id: str
    name: str
    kind: str
    interval_seconds: int
    enabled: bool
    next_run: Optional[str] = None
    last_run: Optional[str] = None
    created_at: Optional[str] = None
