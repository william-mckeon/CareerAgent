"""
src/worker.py

The background job worker for careeragent-jobs.

A single asyncio task, started in the FastAPI lifespan, that runs the queue OFF
the request path: claim a pending job, run its handler, store the result, and
INJECT the result into the job's conversation via careeragent-sessions ("do not
poll"). No agent loop, no model — the handler calls leaf services directly.

Resilience is the whole point of a worker: ONE bad job must never crash the loop
or the service. Every iteration is wrapped, exceptions are logged, and the loop
continues. A handler exception becomes a retry (or a terminal failure at the
attempt cap) via store.retry_or_fail — it never propagates.
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict

logger = logging.getLogger("careeragent-jobs")

Handler = Callable[[Dict[str, Any], Any], Awaitable[str]]


async def _wait(stop_event: asyncio.Event, seconds: float) -> None:
    """Sleep up to ``seconds``, waking immediately if shutdown is requested."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _inject_with_retries(sessions_client: Any, conversation_id: str,
                               summary: str, job_id: str,
                               attempts: int = 3, backoff: float = 1.0) -> bool:
    """Deliver a finished result into the conversation, retrying a TRANSIENT
    sessions failure (a rolling restart / brief 5xx) a few times with backoff.
    The result is already durably stored on the job row, so a permanent failure
    here is logged loudly and remains recoverable via GET /jobs — it just isn't
    auto-delivered. Returns True once injected."""
    for i in range(attempts):
        try:
            status, _ = await sessions_client.inject(conversation_id, "assistant", summary)
            if status == 200:
                return True
            logger.warning("job %s inject attempt %d/%d -> status %s",
                           job_id, i + 1, attempts, status)
        except Exception as exc:
            logger.warning("job %s inject attempt %d/%d failed: %s", job_id, i + 1, attempts, exc)
        if i < attempts - 1:
            await asyncio.sleep(backoff * (i + 1))
    logger.error("job %s result stored but injection FAILED after %d attempts "
                 "(recoverable via GET /jobs/%s)", job_id, attempts, job_id)
    return False


async def execute_job(
    job: Dict[str, Any],
    store: Any,
    handlers: Dict[str, Handler],
    deps: Any,
    sessions_client: Any,
    max_attempts: int,
) -> None:
    """Run ONE already-claimed job to a terminal state.

    Success  -> finish('done', result=summary), then best-effort inject into the
                conversation (the job is done even if injection fails).
    Handler error -> retry_or_fail (re-queue until the attempt cap, then 'failed').
    Unknown kind  -> immediate 'failed' (retrying a missing handler is pointless;
                the API already rejects unknown kinds at enqueue, so this is a
                belt-and-braces guard).
    """
    job_id = job["id"]
    kind = job["kind"]
    handler = handlers.get(kind)
    if handler is None:
        logger.error("job %s has unknown kind '%s' — failing", job_id, kind)
        await store.finish(job_id, "failed", error=f"no handler for kind '{kind}'")
        return

    try:
        summary = await handler(job.get("spec") or {}, deps)
    except Exception as exc:
        logger.warning("job %s (%s) handler error: %s", job_id, kind, exc)
        try:
            await store.retry_or_fail(job_id, str(exc), max_attempts)
        except Exception as store_exc:  # a store blip leaves it 'running' -> startup requeue recovers it
            logger.error("job %s retry_or_fail failed (will be requeued on restart): %s",
                         job_id, store_exc)
        return

    try:
        await store.finish(job_id, "done", result=summary)
    except Exception as store_exc:  # startup requeue_running recovers a stuck 'running' row
        logger.error("job %s finish('done') failed (will be requeued on restart): %s",
                     job_id, store_exc)
        return

    conversation_id = job.get("conversation_id")
    # An EMPTY result means "ran fine, nothing to report" (the #18b recurring
    # reminder kinds return "" when nothing is due) — mark done but do NOT inject,
    # so a quiet scan never spams the Reminders conversation with "nothing to do".
    if conversation_id and summary and summary.strip():
        # Deliver with retries so a transient sessions outage doesn't silently
        # swallow the result (it stays recoverable via GET /jobs regardless).
        await _inject_with_retries(sessions_client, conversation_id, summary, job_id)
    elif conversation_id:
        logger.info("job %s (%s) produced an empty result — skipping inject", job_id, kind)
    logger.info("job %s (%s) done", job_id, kind)


async def run_worker_loop(
    store: Any,
    handlers: Dict[str, Handler],
    deps: Any,
    sessions_client: Any,
    poll_seconds: float = 2.0,
    max_attempts: int = 3,
    *,
    stop_event: asyncio.Event,
) -> None:
    """Claim-and-run until ``stop_event`` is set.

    Each iteration claims one pending job; if the queue is empty, sleep
    ``poll_seconds`` (interruptible by shutdown) and poll again. Every iteration
    is wrapped so a transient store/handler failure logs and continues instead of
    killing the worker.
    """
    logger.info(
        "job worker started (poll=%ss, max_attempts=%s, kinds=%s)",
        poll_seconds, max_attempts, ",".join(sorted(handlers)),
    )
    while not stop_event.is_set():
        try:
            job = await store.claim_one()
            if job is None:
                await _wait(stop_event, poll_seconds)
                continue
            await execute_job(job, store, handlers, deps, sessions_client, max_attempts)
        except asyncio.CancelledError:
            raise  # shutdown — let it propagate so the task ends promptly
        except Exception as exc:
            # A defensive backstop: claim_one / execute_job already handle their
            # own errors, but anything unexpected must not stop the loop.
            logger.error("worker iteration failed (continuing): %s", exc)
            await _wait(stop_event, poll_seconds)
    logger.info("job worker stopped")
