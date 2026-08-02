"""
src/backend/api.py

careeragent-sessions — the conversation system-of-record for CareerAgent.

It sits between careeragent-frontend and careeragent-api: it mints a per-
conversation id, persists the ordered transcript, serves history/restore, and
relays each /chat turn to careeragent-api unchanged (which keeps doing model +
RAG + capture). See specs/0001-sessions.md for the full contract.

Endpoints:
  POST   /chat                       relay + persist (X-API-Key)
  GET    /conversations              list (X-API-Key)
  GET    /conversations/{id}         full transcript (X-API-Key)
  POST   /conversations              mint empty (X-API-Key)
  DELETE /conversations/{id}         remove (X-API-Key)
  GET    /health                     no auth
"""
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from client.api_client import ApiClient
from schemas import (
    AnswerRequest, ChatRequest, InjectRequest, Message, NewConversation, SteerRequest,
)
from security import verify_api_key
from store import Store

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("careeragent-sessions")

ENABLE_DOCS = os.environ.get("SESSIONS_ENABLE_DOCS", "").strip().lower() == "true"

# Module-level singletons, created in lifespan.
store: Optional[Store] = None
api_client: Optional[ApiClient] = None


# ---------------------------------------------------------------------------
# Pure helper: scan a completed SSE event for assistant content + sentinels.
# Kept pure (no I/O) so it is unit-testable. Used to capture the assistant turn
# off the relayed stream without changing what the caller receives.
# ---------------------------------------------------------------------------
def _scan_sse(event_text: str) -> Tuple[str, bool, bool]:
    """Return (assistant_content, saw_done, saw_error) for one SSE event block.

    Completion is signalled by EITHER a non-null ``finish_reason`` (the real
    end-of-generation marker in an OpenAI stream) OR a ``data: [DONE]`` sentinel.
    careeragent-api ends its stream at the finish_reason chunk and does not
    forward [DONE], so relying on [DONE] alone would never mark a turn complete.
    """
    content: List[str] = []
    saw_done = False
    saw_error = False
    for line in event_text.split("\n"):
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            saw_done = True
        elif data.startswith("[ERROR"):
            # Match every in-band error shape the api emits — "[ERROR]",
            # "[ERROR RuntimeError]", "[ERROR upstream=503]". The closing-bracket
            # form "[ERROR]" missed the agent path's "[ERROR {type}]", so an errored
            # turn was mis-persisted as a clean, complete assistant message.
            saw_error = True
        elif data:
            try:
                choice = json.loads(data)["choices"][0]
                piece = choice.get("delta", {}).get("content")
                if piece:
                    content.append(piece)
                if choice.get("finish_reason"):  # "stop"/"length"/... => generation ended
                    saw_done = True
            except Exception:
                pass  # keep-alive lines, partial frames, non-JSON — ignore
    return "".join(content), saw_done, saw_error


def _extract_suspend(event_text: str) -> Optional[dict]:
    """Return the careeragent SUSPEND payload if this SSE event is one, else None.

    THE CROSS-SERVICE CONTRACT (P4): careeragent-api pauses a run by emitting a
    namespaced frame (no OpenAI `choices`, so the frontend/decoder ignore it as
    content) then ending its stream:
        data: {"careeragent": {"event": "suspend",
                               "pending_call_id": "...",
                               "pending_kind": "question" | "approval",
                               "payload": { ...what the frontend renders... },
                               "snapshot": { "convo": [...msgs...], "plan": [...] }}}
    sessions catches it here, persists the run as paused, and forwards the frame so
    the frontend can render the question/approval. The snapshot carries the api's
    accumulated convo (the api is stateless per request) so a later /answer can
    resume by replaying it plus the user's reply."""
    for line in event_text.split("\n"):
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]" or data.startswith("[ERROR"):
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        ca = obj.get("careeragent") if isinstance(obj, dict) else None
        if isinstance(ca, dict) and ca.get("event") == "suspend" and ca.get("pending_call_id"):
            return ca
    return None


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _clean(text: str) -> str:
    """Strip lone UTF-16 surrogates before storing.

    Upstream (gpt-oss via Bedrock/LiteLLM) can split a multi-byte character
    across streamed content deltas, surfacing broken surrogates (e.g. \\udc9d)
    in delta.content. Stored as-is they break Postgres/JSON encoding and 500 on
    retrieval. encode→decode with errors='ignore' drops only the lone surrogates
    and keeps all valid text (proper emoji/curly quotes survive)."""
    return text.encode("utf-8", "ignore").decode("utf-8", "ignore")


def _first_user(messages: List[Message]) -> Optional[str]:
    for m in messages:
        if m.role == "user":
            return m.content
    return None


def _last_user(messages: List[Message]) -> Optional[str]:
    for m in reversed(messages):
        if m.role == "user":
            return m.content
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store, api_client
    store = Store()
    api_client = ApiClient()
    db_ok = await store.ping()
    if db_ok:
        # Idempotently create the P4 run_state table on a pre-P4 DB volume (init.sql
        # only runs on a fresh volume). No-op once it exists.
        try:
            await store.ensure_schema()
        except Exception as exc:  # never block startup on it — /chat still works
            logger.warning("run_state schema ensure failed (interactive channel degraded): %s", exc)
    logger.info("=== careeragent-sessions starting ===")
    logger.info("Port                  : %s", os.environ.get("SESSIONS_PORT", "8005"))
    logger.info("Upstream api          : %s", api_client._url)
    logger.info("DB schema             : %s", store._schema)
    logger.info("Database              : %s", "ok" if db_ok else "UNREACHABLE")
    logger.info("API docs (/docs)      : %s", "enabled" if ENABLE_DOCS else "disabled")
    logger.info("=== careeragent-sessions ready on :%s ===", os.environ.get("SESSIONS_PORT", "8005"))
    yield
    await store.stop()
    logger.info("=== careeragent-sessions shutting down ===")


app = FastAPI(
    title="careeragent-sessions",
    description="Conversation system-of-record for the CareerAgent system.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ---------------------------------------------------------------------------
# POST /chat
# ---------------------------------------------------------------------------
def _stream_turn(conversation_id: str, messages_payload: List[dict],
                 effort: Optional[str], approval: Optional[dict] = None,
                 mode: Optional[str] = None) -> StreamingResponse:
    """Relay ONE coach turn to careeragent-api, scanning the stream for the
    assistant answer, completion, errors, AND a P4 suspend frame. Shared by /chat
    (a fresh turn) and /answer (a resumed turn) so both persist run-state the same
    way — a paused turn saves its snapshot, a clean turn clears the run and stores
    the assistant message. `approval` (P4) carries an in-chat write confirmation
    {call_id, granted} the api executes as it resumes."""
    captured = {"parts": [], "done": False, "errored": False, "suspend": None}

    async def relay() -> AsyncGenerator[bytes, None]:
        buffer = b""
        async for chunk in api_client.stream_chat(
                messages_payload, effort, approval=approval,
                conversation_id=conversation_id, mode=mode):
            # Buffer RAW BYTES and split on the ASCII event delimiter (\n\n); only
            # COMPLETE events (valid UTF-8) are decoded, so a multi-byte char split
            # across chunk boundaries can't corrupt the captured transcript.
            buffer += chunk
            while b"\n\n" in buffer:
                event_bytes, buffer = buffer.split(b"\n\n", 1)
                text = event_bytes.decode("utf-8", errors="replace")
                piece, d, e = _scan_sse(text)
                if piece:
                    captured["parts"].append(piece)
                captured["done"] = captured["done"] or d
                captured["errored"] = captured["errored"] or e
                sus = _extract_suspend(text)
                if sus is not None:
                    captured["suspend"] = sus
            yield chunk  # byte-for-byte to the caller, ONCE per chunk, after scanning
        if buffer.strip():  # final event with no trailing blank line
            text = buffer.decode("utf-8", errors="replace")
            piece, d, e = _scan_sse(text)
            if piece:
                captured["parts"].append(piece)
            captured["done"] = captured["done"] or d
            captured["errored"] = captured["errored"] or e
            captured["suspend"] = captured["suspend"] or _extract_suspend(text)

    async def persist():
        try:
            sus = captured["suspend"]
            if sus is not None:
                # Paused: persist the snapshot + pending request so a later /answer
                # can resume. Do NOT store an assistant message — the turn isn't done.
                # P7 #20: stamp the run's MODE into the snapshot so the resume is
                # server-derived — a reset/forged client mode can never elevate a
                # read-only (plan) run on /answer. (The api's resume only reads
                # snapshot.convo, so this extra key is inert to it.)
                snap = sus.get("snapshot") or {}
                if isinstance(snap, dict) and mode is not None:
                    snap = {**snap, "resume_mode": mode}
                await store.save_run_state(
                    conversation_id, status="paused",
                    snapshot=snap,
                    pending_call_id=sus.get("pending_call_id"),
                    pending_kind=sus.get("pending_kind"),
                    pending_payload=sus.get("payload"),
                )
                return
            if captured["done"] and not captured["errored"] and captured["parts"]:
                await store.add_message(conversation_id, "assistant", _clean("".join(captured["parts"])))
            # A clean or errored end (not a pause) closes any active run.
            if captured["done"] or captured["errored"]:
                await store.clear_run_state(
                    conversation_id, status="interrupted" if captured["errored"] else "complete")
        except Exception as exc:
            logger.error("persist for %s failed: %s", conversation_id, exc)

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Conversation-Id": conversation_id,
        },
        background=BackgroundTask(persist),
    )


@app.post("/chat")
async def chat(request: ChatRequest, api_key: str = Security(verify_api_key)):
    if not request.messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty")
    if not any(m.role == "user" for m in request.messages):
        raise HTTPException(status_code=400, detail="Messages must include at least one user message")

    if request.conversation_id:
        try:
            uuid.UUID(request.conversation_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="conversation_id must be a valid UUID")
        conversation_id = request.conversation_id
    else:
        conversation_id = str(uuid.uuid4())

    title = (_first_user(request.messages) or "").strip()[:60] or None
    await store.upsert_conversation(conversation_id, title)

    # A steering message rides alongside the turn: queue it against the active run
    # so the coach drains it between steps (P4 #15). It does not start a turn itself.
    if request.steer:
        await store.enqueue_steer(conversation_id, _clean(request.steer))

    # Persist only the NEW user turn (the last user message); the frontend resends
    # full history each turn, but the conversation_id keeps continuity.
    new_user = _last_user(request.messages)
    if new_user is not None:
        await store.add_message(conversation_id, "user", _clean(new_user))

    # P4.5: mark the run 'running' so a steer/interrupt can target this live turn
    # (a normal turn otherwise has no run_state row to queue against).
    await store.mark_running(conversation_id)

    messages_payload = [{"role": m.role, "content": m.content} for m in request.messages]
    logger.info("POST /chat | conversation=%s | messages=%d | mode=%s",
                conversation_id, len(request.messages), request.mode or "-")
    return _stream_turn(conversation_id, messages_payload, request.reasoning_effort,
                        mode=request.mode)


# ---------------------------------------------------------------------------
# Run state — the P4 suspend/resume channel
# ---------------------------------------------------------------------------
def _valid_uuid(conversation_id: str) -> None:
    try:
        uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="conversation_id must be a valid UUID")


@app.get("/conversations/{conversation_id}/run-state")
async def get_run_state(conversation_id: str, api_key: str = Security(verify_api_key)):
    """The current run snapshot for a conversation (for resume-on-reload and to
    re-render a pending question/approval). `status: "none"` when there is no run."""
    _valid_uuid(conversation_id)
    rs = await store.get_run_state(conversation_id)
    if rs is None:
        return {"status": "none"}
    return {
        "status": rs["status"],
        "pending_call_id": rs["pending_call_id"],
        "pending_kind": rs["pending_kind"],
        "pending_payload": rs["pending_payload"],
        "updated_at": _iso(rs["updated_at"]),
    }


@app.post("/conversations/{conversation_id}/answer")
async def answer(conversation_id: str, body: AnswerRequest,
                 api_key: str = Security(verify_api_key)):
    """Resolve a paused run with the user's reply and RESUME the same run.

    Atomically claims the pending request iff `body.call_id` matches the active one
    (so a stale/foreign reply can't settle it), then replays the saved convo plus a
    synthetic tool result for the pending call and streams the continuation."""
    _valid_uuid(conversation_id)
    claimed = await store.resolve_pending(conversation_id, body.call_id)
    if claimed is None:
        # No paused run, or the call_id doesn't match the active pending request.
        raise HTTPException(status_code=409,
                            detail="no pending request matches this call_id (already answered, "
                                   "expired, or wrong id)")

    snapshot = claimed.get("snapshot") or {}
    convo = snapshot.get("convo") if isinstance(snapshot, dict) else None
    if not isinstance(convo, list) or not convo:
        raise HTTPException(status_code=422, detail="the paused run has no resumable snapshot")

    kind = claimed.get("pending_kind") or "question"
    # SERVER-DERIVED resume mode (P7 #20): the mode the paused run was in, stamped
    # into the snapshot at suspend — NOT the client's current mode. So a reset (F5),
    # omitted, or forged client mode can never elevate a read-only (plan) run. Only a
    # GENUINELY-granted plan proposal elevates it. body.mode is a fallback for older
    # paused runs that predate the stamp.
    persisted_mode = snapshot.get("resume_mode") if isinstance(snapshot, dict) else None
    run_mode = persisted_mode or body.mode
    low = _clean(body.answer).strip().lower()
    _APPROVE = {"yes", "y", "approve", "approved", "ok", "okay", "confirm", "confirmed",
                "proceed", "go ahead", "do it"}
    _DECLINE = {"no", "n", "not now", "cancel", "decline", "declined", "stop", "nope"}

    # A plan proposal whose reply is NEITHER a bare approve NOR a bare decline is a
    # REVISION — forward the user's text to the coach (staying in the run's mode) so
    # it re-plans, instead of silently inverting it into a decline and dropping it.
    if kind == "plan_proposal" and low not in _APPROVE and low not in _DECLINE:
        await store.add_message(conversation_id, "user", _clean(body.answer))
        resume_messages = list(convo) + [{
            "role": "tool", "tool_call_id": body.call_id,
            "content": ("The user did not simply approve or decline — they asked to REVISE the plan: "
                        f"\"{_clean(body.answer)}\". Update the plan accordingly and propose_plan again "
                        "(still read-only), or ask a brief clarifying question."),
        }]
        logger.info("POST /answer | conversation=%s | plan revision call_id=%s mode=%s",
                    conversation_id, body.call_id, run_mode or "-")
        return _stream_turn(conversation_id, resume_messages, body.reasoning_effort, mode=run_mode)

    if kind in ("approval", "plan_proposal"):
        # A yes/no gate: a write confirmation (approval) OR a plan proposal (P7 #20).
        # Do NOT append a synthetic tool result here — the api settles the pending
        # call as it resumes (executes the write, or seeds the approved plan).
        granted = low in _APPROVE
        await store.add_message(conversation_id, "user", "Approved." if granted else "Declined.")
        # A GRANTED plan is ELEVATED to edit mode server-side (the approval IS the
        # authorization); a DECLINED plan (and every other resume) keeps the run's
        # own persisted mode — a declined plan-mode run stays READ-ONLY.
        resume_mode = "acceptEdits" if (kind == "plan_proposal" and granted) else run_mode
        logger.info("POST /answer | conversation=%s | %s call_id=%s granted=%s mode=%s",
                    conversation_id, kind, body.call_id, granted, resume_mode or "-")
        return _stream_turn(conversation_id, list(convo), body.reasoning_effort,
                            approval={"call_id": body.call_id, "granted": granted},
                            mode=resume_mode)

    # A question. The transcript reads naturally (the answer is a user message), and
    # the saved convo + a synthetic tool result answering the pending call resumes it.
    await store.add_message(conversation_id, "user", _clean(body.answer))
    resume_messages = list(convo) + [{
        "role": "tool",
        "tool_call_id": body.call_id,
        "content": _clean(body.answer),
    }]
    logger.info("POST /answer | conversation=%s | question resume call_id=%s mode=%s",
                conversation_id, body.call_id, run_mode or "-")
    return _stream_turn(conversation_id, resume_messages, body.reasoning_effort, mode=run_mode)


# ---------------------------------------------------------------------------
# Mid-run steering + interrupt (P4.5)
# ---------------------------------------------------------------------------
@app.post("/conversations/{conversation_id}/steer")
async def steer(conversation_id: str, body: SteerRequest,
                api_key: str = Security(verify_api_key)):
    """Queue a steering message against the active run. The coach drains it between
    steps and appends it before its next model call. Does NOT start a turn."""
    _valid_uuid(conversation_id)
    queued = await store.enqueue_steer(conversation_id, _clean(body.message))
    if not queued:
        raise HTTPException(status_code=409, detail="no active run to steer")
    logger.info("POST /steer | conversation=%s", conversation_id)
    return {"queued": True}


@app.post("/conversations/{conversation_id}/interrupt")
async def interrupt(conversation_id: str, api_key: str = Security(verify_api_key)):
    """Ask the active run to stop cleanly at its next step (P4.5)."""
    _valid_uuid(conversation_id)
    ok = await store.request_interrupt(conversation_id)
    if not ok:
        raise HTTPException(status_code=409, detail="no active run to interrupt")
    logger.info("POST /interrupt | conversation=%s", conversation_id)
    return {"interrupt_requested": True}


@app.post("/conversations/{conversation_id}/drain-steer")
async def drain_steer(conversation_id: str, api_key: str = Security(verify_api_key)):
    """INTERNAL — called by careeragent-api between steps: atomically return and
    clear this run's queued steering messages + interrupt flag."""
    _valid_uuid(conversation_id)
    return await store.drain_steer_and_flags(conversation_id)


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------
@app.get("/conversations")
async def list_conversations(
    api_key: str = Security(verify_api_key), limit: int = 50, offset: int = 0
):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    rows = await store.list_conversations(limit, offset)
    return [
        {
            "conversation_id": str(r["id"]),
            "title": r["title"],
            "created_at": _iso(r["created_at"]),
            "updated_at": _iso(r["updated_at"]),
            "message_count": r["message_count"],
        }
        for r in rows
    ]


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, api_key: str = Security(verify_api_key)):
    conv = await store.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {
        "conversation_id": str(conv["id"]),
        "title": conv["title"],
        "created_at": _iso(conv["created_at"]),
        "updated_at": _iso(conv["updated_at"]),
        "messages": [
            {
                "role": m["role"],
                "content": m["content"],
                "idx": m["idx"],
                "created_at": _iso(m["created_at"]),
            }
            for m in conv["messages"]
        ],
    }


@app.post("/conversations")
async def create_conversation(body: NewConversation, api_key: str = Security(verify_api_key)):
    conversation_id = str(uuid.uuid4())
    title = (body.title or "").strip()[:60] or None
    await store.upsert_conversation(conversation_id, title)
    return {"conversation_id": conversation_id}


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, api_key: str = Security(verify_api_key)):
    ok = await store.delete_conversation(conversation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": conversation_id}


# ---------------------------------------------------------------------------
# POST /conversations/{id}/inject — out-of-band message injection (P7 #18)
# ---------------------------------------------------------------------------
# careeragent-jobs posts a completed background job's RESULT here so it lands in
# the conversation the user will see. This does NOT run a turn or touch run_state —
# it only appends a message (the frontend shows it on its next refresh). Same
# X-API-Key boundary as every other route.
@app.post("/conversations/{conversation_id}/inject")
async def inject(conversation_id: str, body: InjectRequest,
                 api_key: str = Security(verify_api_key)):
    _valid_uuid(conversation_id)
    if not await store.conversation_exists(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    # An injected result is always an ASSISTANT note. Forcing the role (ignoring
    # body.role) stops a caller from injecting a spoofed 'system'/'user' turn that
    # the coach would replay as if the human said it (or that breaks Bedrock's
    # alternating-role rule) — the #18a contract only ever injects assistant.
    role = "assistant"
    content = _clean(body.content)
    if not content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    idx = await store.add_message(conversation_id, role, content)
    logger.info("POST /inject | conversation=%s | role=%s | idx=%s | %d chars",
                conversation_id, role, idx, len(content))
    return {"idx": idx}


# ---------------------------------------------------------------------------
# GET /applications/{id}/artifact — download-proxy passthrough (P7 #16)
# ---------------------------------------------------------------------------
# The frontend's front door is sessions, so a rendered-résumé download comes HERE
# and is relayed to careeragent-api's download proxy (which serves the bytes from
# careeragent-dossier). A separate byte-hop from /chat — the file never rides the
# persisted SSE relay. Same X-API-Key (frontend<->sessions) as every other route.
@app.get("/applications/{application_id}/artifact")
async def download_artifact(
    application_id: str,
    artifact_id: Optional[str] = None,
    api_key: str = Security(verify_api_key),
) -> Response:
    if api_client is None:
        raise HTTPException(status_code=503, detail="upstream api not available")
    status_code, content, media_type, disposition = await api_client.get_artifact(
        application_id, artifact_id)
    if status_code != 200 or content is None:
        raise HTTPException(status_code=404, detail="No rendered artifact for this application")
    headers = {"Content-Disposition": disposition} if disposition else {}
    return Response(
        content=content,
        media_type=media_type or "application/octet-stream",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# GET /health  (no auth)
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    db_ok = await store.ping() if store else False
    api_ok = await api_client.health() if api_client else False
    return {
        "status": "ok" if db_ok else "degraded",
        "sessions": "ok",
        "database": "ok" if db_ok else "unreachable",
        "upstream_api": "ok" if api_ok else "unreachable",
    }
