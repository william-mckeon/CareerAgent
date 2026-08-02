"""
src/schemas.py

Request/response models for careeragent-sessions.

The /chat request is intentionally the SAME shape careeragent-frontend already
sends to careeragent-api (messages + optional reasoning_effort), plus an
optional conversation_id — so repointing the frontend at this service needs no
frontend code change.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel


class Message(BaseModel):
    """A single message in the OpenAI messages format."""
    role: str
    content: str


class ChatRequest(BaseModel):
    """
    Body for POST /chat.

    messages         : full OpenAI messages list (frontend sends history each turn).
    reasoning_effort : optional low|medium|high, relayed to careeragent-api.
    conversation_id  : optional UUID. Absent -> sessions mints one. Present ->
                       upsert (continue that conversation).
    """
    messages: List[Message]
    reasoning_effort: Optional[Literal["low", "medium", "high"]] = None
    conversation_id: Optional[str] = None
    # P7 #20 plan-vs-act: the permission mode the user selected (Plan / Edit),
    # relayed to careeragent-api. Absent => the api's server default. Only the two
    # user-facing modes are accepted here (the frontend never offers bypass/default).
    mode: Optional[Literal["plan", "acceptEdits"]] = None
    # P4 interactive channel (all optional — absent => today's behavior):
    #   steer: queue a steering message against the active run (drained mid-turn).
    steer: Optional[str] = None


class AnswerRequest(BaseModel):
    """Body for POST /conversations/{id}/answer — resolve a paused run.

    call_id : the pending request's id (must match the active pending, so one
              user's reply can't settle another's).
    answer  : the user's choice / free text / "yes"|"no" for an approval.
    reasoning_effort : relayed to careeragent-api on the resumed turn.
    """
    call_id: str
    answer: str
    reasoning_effort: Optional[Literal["low", "medium", "high"]] = None
    # P7 #20: the conversation's current mode, so a question/approval resume keeps
    # it. A GRANTED plan_proposal is elevated to acceptEdits server-side (below).
    mode: Optional[Literal["plan", "acceptEdits"]] = None


class SteerRequest(BaseModel):
    """Body for POST /conversations/{id}/steer — queue a mid-run steering message
    (P4.5). Drained by the coach between steps and appended before its next call."""
    message: str


class InjectRequest(BaseModel):
    """Body for POST /conversations/{id}/inject (P7 #18) — an OUT-OF-BAND message
    appended by careeragent-jobs when a background job completes. Not a turn; just
    a message the frontend shows on its next refresh. `role` defaults to assistant."""
    content: str
    role: str = "assistant"


class NewConversation(BaseModel):
    """Body for POST /conversations (mint an empty conversation)."""
    title: Optional[str] = None


class MessageOut(BaseModel):
    role: str
    content: str
    idx: int
    created_at: str


class ConversationSummary(BaseModel):
    conversation_id: str
    title: Optional[str]
    created_at: str
    updated_at: str
    message_count: int


class ConversationDetail(BaseModel):
    conversation_id: str
    title: Optional[str]
    created_at: str
    updated_at: str
    messages: List[MessageOut]
