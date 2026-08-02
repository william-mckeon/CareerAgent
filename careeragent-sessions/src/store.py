"""
src/store.py

Conversation/message persistence for careeragent-sessions.

Own Postgres by default; everything is created in (and queried against) the
SESSIONS_DB_SCHEMA schema (default ``careeragent_sessions``) via the connection
``search_path``, so pointing SESSIONS_DB_HOST/NAME at a SHARED instance later is
a config-only change — no code edit. See specs/0001-sessions.md.

Async SQLAlchemy Core over asyncpg; queries are parameterized (no string
interpolation of values). Tables themselves are created by database/init.sql on
first DB boot — this module only reads/writes rows.
"""
import json
import os
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SCHEMA = os.environ.get("SESSIONS_DB_SCHEMA", "careeragent_sessions")


def _database_url() -> str:
    """Build the asyncpg URL from SESSIONS_DB_* parts, or use SESSIONS_DATABASE_URL."""
    explicit = os.environ.get("SESSIONS_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("SESSIONS_DB_USER", "careeragent_sessions")
    password = os.environ.get("SESSIONS_DB_PASSWORD", "")
    host = os.environ.get("SESSIONS_DB_HOST", "sessions-db")
    port = os.environ.get("SESSIONS_DB_PORT", "5432")
    name = os.environ.get("SESSIONS_DB_NAME", "careeragent_sessions")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


class Store:
    """Thin async persistence layer over the conversations/messages tables."""

    def __init__(self, url: Optional[str] = None, schema: Optional[str] = None):
        self._schema = schema or SCHEMA
        # search_path on every connection pins us to our schema -> a shared
        # instance just needs SESSIONS_DB_NAME pointed at it; no SQL changes.
        self._engine = create_async_engine(
            url or _database_url(),
            pool_pre_ping=True,
            connect_args={"server_settings": {"search_path": self._schema}},
        )

    async def ping(self) -> bool:
        """True if the database is reachable (used by /health)."""
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def ensure_schema(self) -> None:
        """Idempotently create the run_state table on an EXISTING DB volume.

        database/init.sql only runs on a fresh volume, so a DB created before P4
        won't have run_state. Called once at service startup — CREATE ... IF NOT
        EXISTS is a no-op on a fresh DB where init.sql already made it."""
        async with self._engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE IF NOT EXISTS run_state ("
                " conversation_id uuid PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,"
                " status text NOT NULL DEFAULT 'running',"
                " snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,"
                " pending_call_id text, pending_kind text, pending_payload jsonb,"
                " steer_queue jsonb NOT NULL DEFAULT '[]'::jsonb,"
                " updated_at timestamptz NOT NULL DEFAULT now())"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS run_state_paused ON run_state (status) "
                "WHERE status = 'paused'"
            ))
            # P4.5 mid-run interrupt flag — a client sets it, the coach drains it
            # between steps and stops cleanly. Added separately so a pre-P4.5 table
            # gains it without a re-init.
            await conn.execute(text(
                "ALTER TABLE run_state ADD COLUMN IF NOT EXISTS "
                "interrupt_requested boolean NOT NULL DEFAULT false"
            ))

    async def stop(self) -> None:
        await self._engine.dispose()

    async def upsert_conversation(self, conversation_id: str, title: Optional[str]) -> None:
        """Create the conversation if new (keeping the given title), else bump updated_at."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO conversations (id, title) VALUES (:id, :title) "
                    "ON CONFLICT (id) DO UPDATE SET updated_at = now()"
                ),
                {"id": conversation_id, "title": title},
            )

    async def conversation_exists(self, conversation_id: str) -> bool:
        async with self._engine.connect() as conn:
            r = await conn.execute(
                text("SELECT 1 FROM conversations WHERE id = :id"), {"id": conversation_id}
            )
            return r.first() is not None

    async def add_message(self, conversation_id: str, role: str, content: str) -> int:
        """Append a message; the server assigns the next idx atomically. Returns idx.

        A per-conversation transaction advisory lock SERIALIZES concurrent appends
        to the same conversation — otherwise two `MAX(idx)+1` inserts racing (e.g. a
        background job's /inject vs a /chat persist, P7 #18) both compute the same
        idx and one hits the UNIQUE(conversation_id, idx) constraint, dropping a
        message. The lock is held only for this short insert and released on commit."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:cid))"),
                {"cid": conversation_id},
            )
            r = await conn.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, idx, role, content) "
                    "VALUES (gen_random_uuid(), :cid, "
                    "(SELECT COALESCE(MAX(idx), -1) + 1 FROM messages WHERE conversation_id = :cid), "
                    ":role, :content) RETURNING idx"
                ),
                {"cid": conversation_id, "role": role, "content": content},
            )
            idx = r.scalar_one()
            await conn.execute(
                text("UPDATE conversations SET updated_at = now() WHERE id = :cid"),
                {"cid": conversation_id},
            )
            return idx

    async def list_conversations(self, limit: int = 50, offset: int = 0) -> List[dict]:
        async with self._engine.connect() as conn:
            r = await conn.execute(
                text(
                    "SELECT c.id, c.title, c.created_at, c.updated_at, "
                    "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
                    "FROM conversations c ORDER BY c.updated_at DESC LIMIT :limit OFFSET :offset"
                ),
                {"limit": limit, "offset": offset},
            )
            return [dict(row._mapping) for row in r]

    async def get_conversation(self, conversation_id: str) -> Optional[dict]:
        async with self._engine.connect() as conn:
            cr = await conn.execute(
                text("SELECT id, title, created_at, updated_at FROM conversations WHERE id = :id"),
                {"id": conversation_id},
            )
            conv = cr.first()
            if conv is None:
                return None
            mr = await conn.execute(
                text(
                    "SELECT role, content, idx, created_at FROM messages "
                    "WHERE conversation_id = :id ORDER BY idx ASC"
                ),
                {"id": conversation_id},
            )
            out = dict(conv._mapping)
            out["messages"] = [dict(row._mapping) for row in mr]
            return out

    async def delete_conversation(self, conversation_id: str) -> bool:
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text("DELETE FROM conversations WHERE id = :id"), {"id": conversation_id}
            )
            return r.rowcount > 0

    # ----------------------------------------------------------------- run state (P4)
    async def save_run_state(
        self,
        conversation_id: str,
        *,
        status: str,
        snapshot: Dict[str, Any],
        pending_call_id: Optional[str] = None,
        pending_kind: Optional[str] = None,
        pending_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Upsert the ONE active run for a conversation. `status='paused'` carries a
        pending request (call_id + kind + payload); any other status clears it, so a
        resumed/finished run never leaves a stale pending prompt behind."""
        paused = status == "paused"
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO run_state (conversation_id, status, snapshot, "
                    "  pending_call_id, pending_kind, pending_payload, updated_at) "
                    "VALUES (:cid, :status, CAST(:snapshot AS jsonb), :pcid, :pkind, "
                    "  CAST(:ppayload AS jsonb), now()) "
                    "ON CONFLICT (conversation_id) DO UPDATE SET "
                    "  status = EXCLUDED.status, snapshot = EXCLUDED.snapshot, "
                    "  pending_call_id = EXCLUDED.pending_call_id, "
                    "  pending_kind = EXCLUDED.pending_kind, "
                    "  pending_payload = EXCLUDED.pending_payload, updated_at = now()"
                ),
                {
                    "cid": conversation_id, "status": status,
                    "snapshot": json.dumps(snapshot),
                    "pcid": pending_call_id if paused else None,
                    "pkind": pending_kind if paused else None,
                    "ppayload": json.dumps(pending_payload) if (paused and pending_payload) else None,
                },
            )

    async def get_run_state(self, conversation_id: str) -> Optional[dict]:
        """The current run snapshot for a conversation, or None if there is none."""
        async with self._engine.connect() as conn:
            r = await conn.execute(
                text(
                    "SELECT conversation_id, status, snapshot, pending_call_id, "
                    "  pending_kind, pending_payload, steer_queue, updated_at "
                    "FROM run_state WHERE conversation_id = :cid"
                ),
                {"cid": conversation_id},
            )
            row = r.first()
            return dict(row._mapping) if row else None

    async def clear_run_state(self, conversation_id: str, *, status: str = "complete") -> None:
        """Mark a run terminal (kept for history / resume-on-reload checks), clearing
        any pending request and steering queue."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE run_state SET status = :status, pending_call_id = NULL, "
                    "  pending_kind = NULL, pending_payload = NULL, "
                    "  steer_queue = '[]'::jsonb, updated_at = now() "
                    "WHERE conversation_id = :cid"
                ),
                {"cid": conversation_id, "status": status},
            )

    async def resolve_pending(
        self, conversation_id: str, call_id: str
    ) -> Optional[dict]:
        """Atomically claim the pending request iff `call_id` matches the active one,
        flipping status paused->running so a reply can't be replayed and one user's
        answer can't settle another's. Returns {snapshot, steer_queue, pending_kind}
        on success, or None if there's no paused run or the call_id doesn't match.

        SELECT ... FOR UPDATE first, then clear: an UPDATE ... RETURNING pending_kind
        would return the POST-update NULL, losing which KIND (question|approval) the
        answer settles — the /answer endpoint needs that to route the resume."""
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text(
                    "SELECT snapshot, steer_queue, pending_kind FROM run_state "
                    "WHERE conversation_id = :cid AND status = 'paused' "
                    "  AND pending_call_id = :call_id FOR UPDATE"
                ),
                {"cid": conversation_id, "call_id": call_id},
            )
            row = r.first()
            if not row:
                return None
            await conn.execute(
                text(
                    "UPDATE run_state SET status = 'running', pending_call_id = NULL, "
                    "  pending_kind = NULL, pending_payload = NULL, updated_at = now() "
                    "WHERE conversation_id = :cid"
                ),
                {"cid": conversation_id},
            )
            out = dict(row._mapping)
            out["conversation_id"] = conversation_id
            return out

    async def enqueue_steer(self, conversation_id: str, message: str) -> bool:
        """Append a steering message to the active run's queue. Returns False if the
        conversation has no run to steer."""
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text(
                    "UPDATE run_state SET steer_queue = steer_queue || CAST(:msg AS jsonb), "
                    "  updated_at = now() WHERE conversation_id = :cid "
                    "RETURNING conversation_id"
                ),
                {"cid": conversation_id, "msg": json.dumps([message])},
            )
            return r.first() is not None

    async def mark_running(self, conversation_id: str) -> None:
        """Ensure a run_state row exists as 'running' at the START of a turn (P4.5),
        so steering can be queued against it and the coach can drain it. Resets a
        stale interrupt flag from a prior turn; leaves the steer queue (the drain
        clears it). A suspend later overwrites this row with the real snapshot."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO run_state (conversation_id, status) VALUES (:cid, 'running') "
                    "ON CONFLICT (conversation_id) DO UPDATE SET status = 'running', "
                    "  interrupt_requested = false, updated_at = now()"
                ),
                {"cid": conversation_id},
            )

    async def request_interrupt(self, conversation_id: str) -> bool:
        """Flag the active run to stop at its next step (P4.5). Returns False if the
        conversation has no run to interrupt."""
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text("UPDATE run_state SET interrupt_requested = true, updated_at = now() "
                     "WHERE conversation_id = :cid RETURNING conversation_id"),
                {"cid": conversation_id},
            )
            return r.first() is not None

    async def drain_steer_and_flags(self, conversation_id: str) -> Dict[str, Any]:
        """Return AND clear the steering queue + interrupt flag in one atomic read
        (the coach calls this between steps). Read-then-clear under a row lock — an
        UPDATE ... RETURNING would hand back the post-update values and lose them."""
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text("SELECT steer_queue, interrupt_requested FROM run_state "
                     "WHERE conversation_id = :cid FOR UPDATE"),
                {"cid": conversation_id},
            )
            row = r.first()
            if not row:
                return {"messages": [], "interrupted": False}
            q = row._mapping["steer_queue"]
            messages = list(q) if isinstance(q, list) else []
            interrupted = bool(row._mapping["interrupt_requested"])
            if messages or interrupted:
                await conn.execute(
                    text("UPDATE run_state SET steer_queue = '[]'::jsonb, "
                         "interrupt_requested = false, updated_at = now() "
                         "WHERE conversation_id = :cid"),
                    {"cid": conversation_id},
                )
            return {"messages": messages, "interrupted": interrupted}

    async def drain_steer(self, conversation_id: str) -> List[str]:
        """Return AND clear the steering queue (drained between coach steps).

        Read-then-clear under a row lock: an UPDATE ... RETURNING would return the
        POST-update value ('[]'), losing the drained items — so SELECT ... FOR UPDATE
        the current queue first, then empty it in the same transaction so a
        concurrent drain can't double-read the same messages."""
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text("SELECT steer_queue FROM run_state WHERE conversation_id = :cid FOR UPDATE"),
                {"cid": conversation_id},
            )
            row = r.first()
            if not row:
                return []
            q = row._mapping["steer_queue"]
            items = list(q) if isinstance(q, list) else []
            if items:
                await conn.execute(
                    text("UPDATE run_state SET steer_queue = '[]'::jsonb, updated_at = now() "
                         "WHERE conversation_id = :cid"),
                    {"cid": conversation_id},
                )
            return items
