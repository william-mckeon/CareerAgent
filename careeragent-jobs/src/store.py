"""
src/store.py

Job persistence for careeragent-jobs.

Own Postgres by default; everything is created in (and queried against) the
JOBS_DB_SCHEMA schema (default ``careeragent_jobs``) via the connection
``search_path``, so pointing JOBS_DB_HOST/NAME at a SHARED instance later is a
config-only change — no code edit. See specs/0001-jobs.md.

Async SQLAlchemy Core over asyncpg; queries are parameterized (no string
interpolation of values). The ``jobs`` table itself is created by
database/init.sql on first DB boot AND idempotently at startup by
``ensure_schema()`` — this module otherwise only reads/writes rows.
"""
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SCHEMA = os.environ.get("JOBS_DB_SCHEMA", "careeragent_jobs")


def _database_url() -> str:
    """Build the asyncpg URL from JOBS_DB_* parts, or use JOBS_DATABASE_URL."""
    explicit = os.environ.get("JOBS_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("JOBS_DB_USER", "careeragent_jobs")
    password = os.environ.get("JOBS_DB_PASSWORD", "")
    host = os.environ.get("JOBS_DB_HOST", "jobs-db")
    port = os.environ.get("JOBS_DB_PORT", "5432")
    name = os.environ.get("JOBS_DB_NAME", "careeragent_jobs")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


def _iso(value: Any) -> Any:
    """timestamptz -> ISO-8601 string; pass through anything already a str/None."""
    return value.isoformat() if isinstance(value, datetime) else value


def _job_row(m: Any) -> Dict[str, Any]:
    """Normalize a jobs row mapping to a plain dict with str ids + iso timestamps.

    ``spec`` comes back from the SQLAlchemy asyncpg dialect already decoded to a
    Python dict (a jsonb codec is registered on the connection); the isinstance
    guard keeps it robust if a driver ever hands back the raw JSON text."""
    spec = m["spec"]
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except Exception:
            spec = {}
    cid = m["conversation_id"]
    return {
        "id": str(m["id"]),
        "kind": m["kind"],
        "spec": spec if isinstance(spec, dict) else {},
        "conversation_id": str(cid) if cid is not None else None,
        "status": m["status"],
        "attempts": m["attempts"],
        "result": m["result"],
        "error": m["error"],
        "created_at": _iso(m["created_at"]),
        "updated_at": _iso(m["updated_at"]),
    }


class Store:
    """Thin async persistence layer over the ``jobs`` table."""

    def __init__(self, url: Optional[str] = None, schema: Optional[str] = None):
        self._schema = schema or SCHEMA
        # search_path on every connection pins us to our schema -> a shared
        # instance just needs JOBS_DB_NAME pointed at it; no SQL changes.
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
        """Idempotently create the jobs table + indexes on an EXISTING DB volume.

        database/init.sql only runs on a fresh volume, so a DB created before this
        service (or with an older schema) still gets the table here. CREATE ... IF
        NOT EXISTS is a no-op on a fresh DB where init.sql already made it.

        The schema is created first (init.sql normally does this, but a bare
        instance pointed at via JOBS_DATABASE_URL may not have it yet) so the
        unqualified CREATE TABLE lands in our search_path schema regardless."""
        async with self._engine.begin() as conn:
            await conn.execute(text(
                f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"'
            ))
            await conn.execute(text(
                "CREATE TABLE IF NOT EXISTS jobs ("
                " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
                " kind text NOT NULL,"
                " spec jsonb NOT NULL DEFAULT '{}'::jsonb,"
                " conversation_id uuid,"
                " status text NOT NULL DEFAULT 'pending',"
                " attempts integer NOT NULL DEFAULT 0,"
                " result text,"
                " error text,"
                " created_at timestamptz NOT NULL DEFAULT now(),"
                " updated_at timestamptz NOT NULL DEFAULT now())"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS jobs_claimable ON jobs (created_at) "
                "WHERE status = 'pending'"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS jobs_by_conversation "
                "ON jobs (conversation_id, created_at DESC)"
            ))
            # P7 #18b — recurring schedules + a tiny key/value settings table (holds
            # the singleton "Reminders" conversation id the scheduler injects into).
            await conn.execute(text(
                "CREATE TABLE IF NOT EXISTS schedules ("
                " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
                " name text UNIQUE NOT NULL,"          # stable seed key (e.g. 'follow_up_scan')
                " kind text NOT NULL,"                 # the job kind this schedule enqueues
                " spec jsonb NOT NULL DEFAULT '{}'::jsonb,"
                " interval_seconds integer NOT NULL,"
                " next_run timestamptz NOT NULL DEFAULT now(),"
                " enabled boolean NOT NULL DEFAULT true,"
                " last_run timestamptz,"
                " created_at timestamptz NOT NULL DEFAULT now())"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS schedules_due ON schedules (next_run) "
                "WHERE enabled"
            ))
            await conn.execute(text(
                "CREATE TABLE IF NOT EXISTS jobs_settings ("
                " key text PRIMARY KEY, value text)"
            ))

    async def stop(self) -> None:
        await self._engine.dispose()

    # ----------------------------------------------------------------- writes
    async def enqueue(
        self, kind: str, spec: Dict[str, Any], conversation_id: Optional[str]
    ) -> Dict[str, Any]:
        """INSERT a pending job. Returns {id, status}. ``spec`` is stored as jsonb
        (json.dumps + CAST) so the worker reads it back as a Python dict."""
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text(
                    "INSERT INTO jobs (kind, spec, conversation_id) "
                    "VALUES (:kind, CAST(:spec AS jsonb), :cid) "
                    "RETURNING id, status"
                ),
                {"kind": kind, "spec": json.dumps(spec or {}), "cid": conversation_id},
            )
            row = r.first()
            return {"id": str(row._mapping["id"]), "status": row._mapping["status"]}

    async def requeue_running(self) -> int:
        """Reset orphaned 'running' jobs back to 'pending' so they re-run. A worker
        crash / container redeploy / OOM-kill mid-job leaves a row durably 'running'
        that claim_one (pending-only) would never re-select — orphaning it forever.
        With a SINGLE worker, any 'running' row at startup is by definition a
        leftover, so it's safe to requeue them all here BEFORE the worker starts.
        (attempts is preserved, so max_attempts still bounds a job that reliably
        crashes the worker.) Returns the number requeued."""
        async with self._engine.begin() as conn:
            r = await conn.execute(text(
                "UPDATE jobs SET status = 'pending', updated_at = now() "
                "WHERE status = 'running' RETURNING id"
            ))
            return len(r.fetchall())

    async def claim_one(self) -> Optional[Dict[str, Any]]:
        """Atomically claim ONE pending job (oldest first), flipping it to
        'running' and bumping attempts. FOR UPDATE SKIP LOCKED lets multiple
        workers claim disjoint jobs without blocking. Returns the row dict or
        None when the queue is empty."""
        async with self._engine.begin() as conn:
            r = await conn.execute(text(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, "
                "  updated_at = now() "
                "WHERE id = (SELECT id FROM jobs WHERE status = 'pending' "
                "            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                "RETURNING *"
            ))
            row = r.first()
            return _job_row(row._mapping) if row else None

    async def finish(
        self,
        job_id: str,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Set a terminal status ('done'|'failed') plus result/error + updated_at."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE jobs SET status = :status, result = :result, "
                    "  error = :error, updated_at = now() WHERE id = :id"
                ),
                {"status": status, "result": result, "error": error, "id": job_id},
            )

    async def retry_or_fail(self, job_id: str, error: str, max_attempts: int) -> None:
        """Re-queue for another run if attempts is still under the cap, else fail.

        ``attempts`` was already incremented by claim_one, so after N failures the
        row has attempts = N. With max_attempts = 3 the job runs at attempts
        1, 2, 3 and is marked 'failed' on the 3rd failure."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE jobs SET "
                    "  status = CASE WHEN attempts < :max THEN 'pending' ELSE 'failed' END, "
                    "  error = :error, updated_at = now() WHERE id = :id"
                ),
                {"max": max_attempts, "error": error, "id": job_id},
            )

    # ----------------------------------------------------------------- reads
    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        async with self._engine.connect() as conn:
            r = await conn.execute(
                text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}
            )
            row = r.first()
            return _job_row(row._mapping) if row else None

    async def list_jobs(
        self,
        conversation_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Newest-first, optionally filtered. None-valued filters are dropped."""
        clauses: List[str] = []
        params: Dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if conversation_id is not None:
            clauses.append("conversation_id = :cid")
            params["cid"] = conversation_id
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self._engine.connect() as conn:
            r = await conn.execute(
                text(
                    "SELECT * FROM jobs" + where +
                    " ORDER BY created_at DESC LIMIT :limit"
                ),
                params,
            )
            return [_job_row(row._mapping) for row in r]

    # ------------------------------------------------------- schedules (P7 #18b)
    async def seed_default_schedules(self, defaults: List[Dict[str, Any]]) -> None:
        """Insert the default recurring schedules once. ON CONFLICT (name) DO NOTHING
        so re-seeding on every boot is a no-op and a schedule an operator later
        disabled or retuned is NEVER overwritten. next_run defaults to now(), so a
        freshly-seeded schedule runs shortly after first startup."""
        async with self._engine.begin() as conn:
            for d in defaults:
                await conn.execute(
                    text(
                        "INSERT INTO schedules (name, kind, spec, interval_seconds) "
                        "VALUES (:name, :kind, CAST(:spec AS jsonb), :interval) "
                        "ON CONFLICT (name) DO NOTHING"
                    ),
                    {"name": d["name"], "kind": d["kind"],
                     "spec": json.dumps(d.get("spec") or {}), "interval": int(d["interval_seconds"])},
                )

    async def due_schedules(self) -> List[Dict[str, Any]]:
        """Enabled schedules whose next_run has arrived (oldest first)."""
        async with self._engine.connect() as conn:
            r = await conn.execute(text(
                "SELECT id, name, kind, spec, interval_seconds, next_run, enabled "
                "FROM schedules WHERE enabled AND next_run <= now() ORDER BY next_run"
            ))
            out: List[Dict[str, Any]] = []
            for row in r:
                m = row._mapping
                spec = m["spec"]
                if isinstance(spec, str):
                    try:
                        spec = json.loads(spec)
                    except Exception:
                        spec = {}
                out.append({"id": str(m["id"]), "name": m["name"], "kind": m["kind"],
                            "spec": spec if isinstance(spec, dict) else {},
                            "interval_seconds": m["interval_seconds"]})
            return out

    async def advance_schedule(self, schedule_id: str, interval_seconds: int) -> None:
        """Move next_run to now()+interval (NOT last+interval) so a scheduler that
        was down for a while doesn't fire a catch-up STORM of missed runs."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE schedules SET last_run = now(), "
                    "  next_run = now() + make_interval(secs => :interval) "
                    "WHERE id = :id"
                ),
                {"interval": int(interval_seconds), "id": schedule_id},
            )

    async def list_schedules(self) -> List[Dict[str, Any]]:
        async with self._engine.connect() as conn:
            r = await conn.execute(text(
                "SELECT id, name, kind, interval_seconds, next_run, enabled, last_run, created_at "
                "FROM schedules ORDER BY name"
            ))
            return [
                {"id": str(m["id"]), "name": m["name"], "kind": m["kind"],
                 "interval_seconds": m["interval_seconds"], "enabled": m["enabled"],
                 "next_run": _iso(m["next_run"]), "last_run": _iso(m["last_run"]),
                 "created_at": _iso(m["created_at"])}
                for m in (row._mapping for row in r)
            ]

    # -------------------------------------------------------- settings (k/v)
    async def get_setting(self, key: str) -> Optional[str]:
        async with self._engine.connect() as conn:
            r = await conn.execute(
                text("SELECT value FROM jobs_settings WHERE key = :k"), {"k": key}
            )
            row = r.first()
            return row._mapping["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO jobs_settings (key, value) VALUES (:k, :v) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                ),
                {"k": key, "v": value},
            )
