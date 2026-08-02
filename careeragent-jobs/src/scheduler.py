"""
src/scheduler.py

The recurring-job scheduler for careeragent-jobs (P7 #18b — cron / reminders).

A single asyncio task, started in the FastAPI lifespan alongside the worker, that
turns TIME into WORK. Every ``tick_seconds`` it asks the store for schedules whose
``next_run`` has arrived and, for each, ENQUEUES a job into the SAME jobs table the
worker drains — targeted at the singleton "🔔 Reminders" conversation — then
advances that schedule's ``next_run``. It never runs a handler itself: it only
enqueues; the worker runs the job, and the worker's empty-result skip keeps a scan
that finds nothing due from spamming the Reminders conversation.

Design notes:
  * Seed-once. Default schedules are inserted ON CONFLICT DO NOTHING at startup, so
    redeploys never overwrite an operator's later enable/disable/retune.
  * Fail-soft. A tick is fully wrapped; any error logs and the loop continues. A
    per-schedule enqueue failure is caught so one bad schedule can't starve the
    others.
  * Defer, don't drop. If the Reminders conversation can't be resolved (sessions
    down), the tick does NOT advance the due schedules — it retries next tick, so a
    sessions outage delays reminders instead of silently skipping them.
  * No catch-up storm. advance_schedule sets next_run = now()+interval (not
    last+interval), so a scheduler that was down for a day fires each schedule ONCE
    on return, not once per missed interval.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("careeragent-jobs")

REMINDERS_CONVERSATION_KEY = "reminders_conversation_id"
REMINDERS_TITLE = "🔔 Reminders"


def default_schedules(follow_up_interval: int, freshness_interval: int) -> List[Dict[str, Any]]:
    """The recurring reminders seeded once on first boot. Cadence is config
    (env-driven) so an operator tunes it without a migration. The ``name`` is the
    stable seed key; the ``kind`` is the job the worker will run."""
    return [
        {"name": "follow_up_scan", "kind": "follow_up_scan", "spec": {},
         "interval_seconds": int(follow_up_interval)},
        {"name": "resume_freshness", "kind": "resume_freshness", "spec": {},
         "interval_seconds": int(freshness_interval)},
    ]


async def _wait(stop_event: asyncio.Event, seconds: float) -> None:
    """Sleep up to ``seconds``, waking immediately if shutdown is requested."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _persist_reminders_id(store: Any, cid: str) -> None:
    """Persist the resolved Reminders id BEST-EFFORT. A jobs-DB write failure here
    (read-only replica, disk full) must NOT propagate: the conversation already
    exists in sessions, and reconcile-by-title re-adopts it next tick — so a failed
    persist can never fork or orphan a thread. It only means we re-resolve by title
    until a write lands."""
    try:
        await store.set_setting(REMINDERS_CONVERSATION_KEY, cid)
    except Exception as exc:
        logger.warning("could not persist reminders conversation id %s (will re-resolve "
                       "by title next tick): %s", cid, exc)


async def _find_reminders_by_title(sessions_client: Any) -> Optional[str]:
    """Adopt an EXISTING "🔔 Reminders" conversation by title, if one exists. This
    is the self-healing dedup that keeps the "singleton" singular: a create whose
    response was lost (timeout) — or that succeeded but wasn't persisted — leaves a
    real thread in sessions, and this finds it instead of minting a second one.
    Deterministically returns the OLDEST match (stable across ticks; ISO-8601
    created_at sorts chronologically as a string), or None."""
    status, body = await sessions_client.list_conversations()
    if status != 200 or not isinstance(body, list):
        return None
    matches = [c for c in body if isinstance(c, dict)
               and str(c.get("title") or "").strip().startswith(REMINDERS_TITLE)]
    if not matches:
        return None
    matches.sort(key=lambda c: (str(c.get("created_at") or ""),
                                str(c.get("conversation_id") or "")))
    chosen = matches[0].get("conversation_id")
    return str(chosen) if chosen else None


async def ensure_reminders_conversation(store: Any, sessions_client: Any) -> Optional[str]:
    """Resolve the id of the singleton "🔔 Reminders" conversation. Returns None if
    sessions is unreachable and no thread can be resolved — the caller then defers
    this tick (never advancing, so no reminder is dropped).

    Resolution order (idempotent / self-healing):
      1. A persisted id that still EXISTS (200) → reuse it (the hot path).
         A transient verify error (0 / 5xx) on a persisted id → reuse it too (don't
         churn on a blip). Only a 404 (user deleted it) falls through.
      2. Otherwise ADOPT an existing "🔔 Reminders" thread by title, if any — so a
         create whose response was lost, or that wasn't persisted, is reclaimed
         instead of forking a duplicate.
      3. Otherwise CREATE a fresh one. Persistence is best-effort (see
         _persist_reminders_id) so a write outage can't orphan the new thread."""
    cid = await store.get_setting(REMINDERS_CONVERSATION_KEY)
    if cid:
        status, _ = await sessions_client.get_conversation(cid)
        if status == 200:
            return cid
        if status == 404:
            logger.info("reminders conversation %s was deleted — re-resolving", cid)
            # fall through to reconcile-by-title / create
        else:
            # Transient (0 / 5xx): do NOT churn — reuse the id we already have so a
            # brief sessions blip doesn't fork the thread.
            logger.warning("could not verify reminders conversation %s (status=%s) — reusing",
                           cid, status)
            return cid

    # No usable persisted id. Adopt an existing thread by title before creating one
    # — this collapses any prior orphan back to a single Reminders conversation.
    found = await _find_reminders_by_title(sessions_client)
    if found:
        logger.info("adopted existing reminders conversation %s by title", found)
        await _persist_reminders_id(store, found)
        return found

    status, body = await sessions_client.create_conversation(REMINDERS_TITLE)
    if status == 200 and isinstance(body, dict) and body.get("conversation_id"):
        new_cid = str(body["conversation_id"])
        logger.info("created reminders conversation %s", new_cid)
        await _persist_reminders_id(store, new_cid)
        return new_cid
    logger.warning("could not create reminders conversation (status=%s) — reminders deferred",
                   status)
    return None


async def _tick(store: Any, sessions_client: Any) -> None:
    """One scheduler pass: enqueue a job for each due schedule, then advance it."""
    due = await store.due_schedules()
    if not due:
        return
    cid = await ensure_reminders_conversation(store, sessions_client)
    if not cid:
        # Nowhere to deliver — hold off WITHOUT advancing so we retry next tick.
        logger.warning("%d schedule(s) due but Reminders conversation unavailable — deferring",
                       len(due))
        return
    for sched in due:
        try:
            row = await store.enqueue(sched["kind"], sched.get("spec") or {}, cid)
            # Advance AFTER a successful enqueue so a failed enqueue is retried (a
            # rare duplicate on an advance-only failure is preferable to a silently
            # skipped reminder).
            await store.advance_schedule(sched["id"], sched["interval_seconds"])
            logger.info("scheduler enqueued %s (schedule=%s, job=%s) -> conversation %s",
                        sched["kind"], sched["name"], row.get("id"), cid)
        except Exception as exc:
            logger.error("scheduler failed on schedule %s (will retry next tick): %s",
                         sched.get("name"), exc)


async def run_scheduler_loop(
    store: Any,
    sessions_client: Any,
    defaults: List[Dict[str, Any]],
    tick_seconds: float = 60.0,
    *,
    stop_event: asyncio.Event,
) -> None:
    """Seed the default schedules once, then tick until ``stop_event`` is set."""
    try:
        await store.seed_default_schedules(defaults)
    except Exception as exc:  # a seed failure must not stop the loop (rows may exist)
        logger.warning("scheduler seed failed (continuing): %s", exc)

    logger.info("scheduler started (tick=%ss, schedules=%s)",
                tick_seconds, ",".join(d["name"] for d in defaults))
    while not stop_event.is_set():
        try:
            await _tick(store, sessions_client)
        except asyncio.CancelledError:
            raise  # shutdown — propagate so the task ends promptly
        except Exception as exc:
            logger.error("scheduler tick failed (continuing): %s", exc)
        await _wait(stop_event, tick_seconds)
    logger.info("scheduler stopped")
