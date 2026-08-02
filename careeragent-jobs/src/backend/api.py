"""
src/backend/api.py

careeragent-jobs — the background/async job runner for CareerAgent (P7 #18a).

careeragent-api ENQUEUES a slow task here (POST /jobs) and returns immediately;
a WORKER loop (src/worker.py, started in this lifespan) claims it, runs it off
the request path, stores the result, and INJECTS the result as an assistant
message into the job's conversation via careeragent-sessions — so the user sees
it appear without polling. No agent loop and no model live here; the worker
calls leaf services (careeragent-review) directly.

Endpoints:
  POST /jobs           enqueue a job                       (X-API-Key)
  GET  /jobs/{id}      job status/result                   (X-API-Key)
  GET  /jobs           list jobs (filter by conv/status)   (X-API-Key)
  GET  /health         db status                           (no auth)
"""
import asyncio
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.responses import JSONResponse

from client.code import CodeClient
from client.dossier import DossierClient
from client.review import ReviewClient
from client.sessions import SessionsClient
from jobtypes import HANDLERS, Deps
from scheduler import default_schedules, run_scheduler_loop
from schemas import JobCreate
from security import verify_api_key
from store import Store
from worker import run_worker_loop

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("careeragent-jobs")

ENABLE_DOCS = os.environ.get("JOBS_ENABLE_DOCS", "").strip().lower() == "true"
PORT = os.environ.get("JOBS_PORT", "8011")

REVIEW_URL = os.environ.get("REVIEW_URL", "http://careeragent-review:8007").strip().rstrip("/")
REVIEW_API_KEY = os.environ.get("REVIEW_API_KEY", "").strip()
SESSIONS_URL = os.environ.get("SESSIONS_URL", "http://careeragent-sessions:8005").strip().rstrip("/")
SESSIONS_API_KEY = os.environ.get("SESSIONS_API_KEY", "").strip()
DOSSIER_URL = os.environ.get("DOSSIER_URL", "http://careeragent-dossier:8006").strip().rstrip("/")
DOSSIER_API_KEY = os.environ.get("DOSSIER_API_KEY", "").strip()
# careeragent-code (Slice E, OPTIONAL) — the nightly repo cache-warm target. Jobs
# carries only CODE_API_KEY; the GitHub PAT stays inside careeragent-code.
CODE_URL = os.environ.get("CODE_URL", "http://careeragent-code:8012").strip().rstrip("/")
CODE_API_KEY = os.environ.get("CODE_API_KEY", "").strip()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


POLL_SECONDS = float(_int_env("JOBS_WORKER_POLL_SECONDS", 2))
MAX_ATTEMPTS = _int_env("JOBS_MAX_ATTEMPTS", 3)

# Scheduler (P7 #18b). Disable-able as a whole; cadence + per-schedule intervals
# are config so an operator tunes them without a migration. Defaults: check every
# 60s; both reminder scans run daily.
SCHEDULER_ENABLED = os.environ.get("JOBS_SCHEDULER_ENABLED", "true").strip().lower() != "false"
SCHEDULER_TICK_SECONDS = float(_int_env("JOBS_SCHEDULER_TICK_SECONDS", 60))
FOLLOW_UP_INTERVAL_SECONDS = _int_env("JOBS_FOLLOW_UP_INTERVAL_SECONDS", 86400)
FRESHNESS_INTERVAL_SECONDS = _int_env("JOBS_FRESHNESS_INTERVAL_SECONDS", 86400)
# Slice E — nightly repo cache-warm cadence (kind repo_presync). Seeded only when
# careeragent-code is configured.
PRESYNC_INTERVAL_SECONDS = _int_env("JOBS_PRESYNC_INTERVAL_SECONDS", 86400)

# Module-level singletons, created in lifespan.
store: Optional[Store] = None
review_client: Optional[ReviewClient] = None
sessions_client: Optional[SessionsClient] = None
dossier_client: Optional[DossierClient] = None
code_client: Optional[CodeClient] = None
_worker_task: Optional[asyncio.Task] = None
_scheduler_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None


def _public(job: dict) -> dict:
    """The documented public view of a job row (spec is worker-internal)."""
    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "attempts": job["attempts"],
        "result": job["result"],
        "error": job["error"],
        "conversation_id": job["conversation_id"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store, review_client, sessions_client, dossier_client, code_client
    global _worker_task, _scheduler_task, _stop_event
    store = Store()
    db_ok = await store.ping()
    if db_ok:
        # Idempotently create the jobs table on an existing DB volume (init.sql
        # only runs on a fresh volume). No-op once it exists.
        try:
            await store.ensure_schema()
            # Requeue any job orphaned in 'running' by a previous crash / redeploy /
            # OOM-kill (single worker -> a leftover 'running' row can only be an
            # orphan). Do this BEFORE the worker starts so it re-claims them.
            n = await store.requeue_running()
            if n:
                logger.info("requeued %d orphaned 'running' job(s) -> pending", n)
        except Exception as exc:  # never block startup on it
            logger.warning("jobs schema ensure/requeue failed: %s", exc)

    # Build outbound clients defensively — a missing key must not stop the API
    # (POST /jobs / GET still work); only the worker's ability to run is degraded.
    try:
        review_client = ReviewClient(url=REVIEW_URL, api_key=REVIEW_API_KEY)
        await review_client.start()
    except Exception as exc:
        review_client = None
        logger.warning("ReviewClient unavailable (review jobs will fail): %s", exc)
    try:
        sessions_client = SessionsClient(url=SESSIONS_URL, api_key=SESSIONS_API_KEY)
        await sessions_client.start()
    except Exception as exc:
        sessions_client = None
        logger.warning("SessionsClient unavailable (results won't inject): %s", exc)
    try:
        dossier_client = DossierClient(url=DOSSIER_URL, api_key=DOSSIER_API_KEY)
        await dossier_client.start()
    except Exception as exc:
        dossier_client = None
        logger.warning("DossierClient unavailable (reminder jobs will fail): %s", exc)
    # careeragent-code (Slice E) is OPTIONAL — build it only if a key is configured,
    # defensively, so a missing/broken code service never blocks startup.
    if CODE_API_KEY:
        try:
            code_client = CodeClient(url=CODE_URL, api_key=CODE_API_KEY)
            await code_client.start()
        except Exception as exc:
            code_client = None
            logger.warning("CodeClient unavailable (repo pre-sync will fail): %s", exc)
    else:
        logger.info("careeragent-code not configured (CODE_API_KEY unset) — no repo pre-sync")

    _stop_event = asyncio.Event()
    if review_client is not None and sessions_client is not None:
        # dossier_client / code_client may be None — review_repos still works; only the
        # reminder kinds need dossier and only repo_presync needs code, and each raises
        # cleanly (→ retry/fail) if its client is absent.
        deps = Deps(review=review_client, dossier=dossier_client, code=code_client)
        _worker_task = asyncio.create_task(
            run_worker_loop(store, HANDLERS, deps, sessions_client,
                            POLL_SECONDS, MAX_ATTEMPTS, stop_event=_stop_event)
        )
    else:
        logger.warning("worker NOT started — a required outbound client is missing")

    # Scheduler (P7 #18b + Slice E) — enqueues recurring jobs. Requires the worker (to
    # drain what it enqueues) and sessions (the Reminders conversation). Each recurring
    # KIND is seeded only when the client it needs is present: the reminder scans need
    # dossier, the repo_presync warm needs code. A 0 interval → that kind is not seeded,
    # so the scheduler never enqueues work nothing can run. Start it as long as at least
    # ONE kind is runnable (so a dossier outage doesn't also pause the code warm).
    if (SCHEDULER_ENABLED and _worker_task is not None and sessions_client is not None
            and (dossier_client is not None or code_client is not None)):
        defaults = default_schedules(
            FOLLOW_UP_INTERVAL_SECONDS if dossier_client is not None else 0,
            FRESHNESS_INTERVAL_SECONDS if dossier_client is not None else 0,
            PRESYNC_INTERVAL_SECONDS if code_client is not None else 0,
        )
        _scheduler_task = asyncio.create_task(
            run_scheduler_loop(store, sessions_client, defaults,
                               SCHEDULER_TICK_SECONDS, stop_event=_stop_event)
        )
    elif SCHEDULER_ENABLED:
        logger.warning("scheduler NOT started — worker/sessions up and dossier-or-code needed")
    else:
        logger.info("scheduler disabled (JOBS_SCHEDULER_ENABLED=false)")

    logger.info("=== careeragent-jobs starting ===")
    logger.info("Port                  : %s", PORT)
    logger.info("DB schema             : %s", store._schema)
    logger.info("Database              : %s", "ok" if db_ok else "UNREACHABLE")
    logger.info("Review upstream       : %s", REVIEW_URL)
    logger.info("Sessions upstream     : %s", SESSIONS_URL)
    logger.info("Dossier upstream      : %s", DOSSIER_URL)
    logger.info("Code upstream         : %s", CODE_URL if CODE_API_KEY else "not configured")
    logger.info("Worker                : %s (poll=%ss, max_attempts=%s)",
                "running" if _worker_task else "disabled", POLL_SECONDS, MAX_ATTEMPTS)
    logger.info("Scheduler             : %s (tick=%ss, follow_up=%ss, freshness=%ss, presync=%s)",
                "running" if _scheduler_task else "disabled",
                SCHEDULER_TICK_SECONDS, FOLLOW_UP_INTERVAL_SECONDS, FRESHNESS_INTERVAL_SECONDS,
                f"{PRESYNC_INTERVAL_SECONDS}s" if code_client is not None else "off")
    logger.info("API docs (/docs)      : %s", "enabled" if ENABLE_DOCS else "disabled")
    logger.info("=== careeragent-jobs ready on :%s ===", PORT)
    yield

    # Shutdown: stop the background tasks cleanly, then dispose clients + engine.
    if _stop_event is not None:
        _stop_event.set()
    for name, task in (("worker", _worker_task), ("scheduler", _scheduler_task)):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("%s task ended with error: %s", name, exc)
    if review_client is not None:
        await review_client.stop()
    if sessions_client is not None:
        await sessions_client.stop()
    if dossier_client is not None:
        await dossier_client.stop()
    if code_client is not None:
        await code_client.stop()
    await store.stop()
    logger.info("=== careeragent-jobs shutting down ===")


app = FastAPI(
    title="careeragent-jobs",
    description="Background/async job runner for the CareerAgent system.",
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


def _require_store() -> Store:
    if store is None:
        raise HTTPException(status_code=503, detail="job store not available")
    return store


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _worker_running() -> bool:
    """True only if the worker task is alive — a job enqueued while it's disabled
    (a missing outbound key) would sit 'pending' forever and the promised update
    would never arrive, so POST /jobs rejects rather than accept dead work."""
    return _worker_task is not None and not _worker_task.done()


# ---------------------------------------------------------------------------
# POST /jobs — enqueue
# ---------------------------------------------------------------------------
@app.post("/jobs", status_code=201)
async def create_job(body: JobCreate, api_key: str = Security(verify_api_key)):
    if body.kind not in HANDLERS:
        raise HTTPException(status_code=400, detail=f"unknown job kind '{body.kind}'")
    if body.conversation_id is not None and not _valid_uuid(body.conversation_id):
        raise HTTPException(status_code=400, detail="conversation_id must be a valid UUID")
    if not _worker_running():
        raise HTTPException(status_code=503,
                            detail="the job worker is not running; jobs can't be processed right now")
    st = _require_store()
    row = await st.enqueue(body.kind, body.spec or {}, body.conversation_id)
    logger.info("POST /jobs | kind=%s | id=%s | conversation=%s",
                body.kind, row["id"], body.conversation_id or "-")
    return {"id": row["id"], "status": row["status"]}


# ---------------------------------------------------------------------------
# GET /jobs/{id} — status/result
# ---------------------------------------------------------------------------
@app.get("/jobs/{job_id}")
async def get_job(job_id: str, api_key: str = Security(verify_api_key)):
    st = _require_store()
    if not _valid_uuid(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = await st.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _public(job)


# ---------------------------------------------------------------------------
# GET /jobs — list (newest first), optional conversation_id / status filters
# ---------------------------------------------------------------------------
@app.get("/jobs")
async def list_jobs(
    conversation_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    api_key: str = Security(verify_api_key),
) -> List[dict]:
    if conversation_id is not None and not _valid_uuid(conversation_id):
        raise HTTPException(status_code=400, detail="conversation_id must be a valid UUID")
    st = _require_store()
    limit = max(1, min(limit, 100))  # cap the page size
    rows = await st.list_jobs(conversation_id=conversation_id, status=status, limit=limit)
    return [_public(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /schedules — the recurring schedules (read-only observability, P7 #18b)
# ---------------------------------------------------------------------------
@app.get("/schedules")
async def list_schedules(api_key: str = Security(verify_api_key)) -> List[dict]:
    st = _require_store()
    return await st.list_schedules()


# ---------------------------------------------------------------------------
# GET /health — no auth
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    db_ok = await store.ping() if store else False
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "careeragent-jobs",
        "database": "ok" if db_ok else "unreachable",
        "worker": "running" if _worker_running() else "stopped",
        "scheduler": "running" if (_scheduler_task is not None and not _scheduler_task.done()) else "stopped",
    }
