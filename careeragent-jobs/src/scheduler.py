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

# Kinds whose handler returns "" and never injects a result — they need NO
# conversation, so the scheduler enqueues them with conversation_id=None. This
# decouples them from sessions (a sessions blip can't defer a silent cache-warm)
# and avoids minting an empty "🔔 Reminders" thread on a reminders-less install.
_CONVERSATIONLESS_KINDS = {"repo_presync"}


def default_schedules(follow_up_interval: int, freshness_interval: int,
                      presync_interval: int = 0) -> List[Dict[str, Any]]:
    """The recurring jobs seeded once on first boot. Cadence is config (env-driven)
    so an operator tunes it without a migration. The ``name`` is the stable seed key;
    the ``kind`` is the job the worker will run.

    An interval of 0 means "do NOT seed this kind" — the caller passes 0 when the
    client that kind needs is absent (dossier for the two reminder scans, careeragent-
    code for the Slice E ``repo_presync`` warm), so no schedule is ever seeded that
    would only fail. ``presync_interval`` defaults to 0, so existing two-arg callers
    are unchanged (they seed exactly the two reminder scans)."""
    candidates = [
        ("follow_up_scan", "follow_up_scan", follow_up_interval),
        ("resume_freshness", "resume_freshness", freshness_interval),
        ("repo_presync", "repo_presync", presync_interval),
    ]
    return [{"name": name, "kind": kind, "spec": {}, "interval_seconds": int(interval)}
            for (name, kind, interval) in candidates if interval and int(interval) > 0]


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


async def _enqueue_and_advance(store: Any, sched: Dict[str, Any], cid: Optional[str]) -> None:
    """Enqueue one due schedule's job (to conversation ``cid``, which is None for a
    conversation-less/silent kind) then advance its next_run. Advancing AFTER a
    successful enqueue means a failed enqueue is retried next tick (a rare duplicate on
    an advance-only failure beats a silently skipped run)."""
    row = await store.enqueue(sched["kind"], sched.get("spec") or {}, cid)
    await store.advance_schedule(sched["id"], sched["interval_seconds"])
    logger.info("scheduler enqueued %s (schedule=%s, job=%s) -> conversation %s",
                sched["kind"], sched["name"], row.get("id"), cid or "(none)")


async def _tick(store: Any, sessions_client: Any, runnable_kinds: Optional[set] = None) -> None:
    """One scheduler pass: enqueue a job for each due, runnable schedule, then advance it.

    ``runnable_kinds`` (None = run everything, for back-compat) is the set of kinds whose
    handler client is present THIS boot. A due schedule whose kind is NOT runnable is
    advanced but NOT enqueued — so a seeded-but-client-less kind (e.g. a reminder left
    over from a boot when dossier was configured) doesn't fire-and-fail every interval."""
    due = await store.due_schedules()
    if not due:
        return

    if runnable_kinds is not None:
        for sched in due:
            if sched["kind"] not in runnable_kinds:
                try:
                    await store.advance_schedule(sched["id"], sched["interval_seconds"])
                    logger.info("scheduler skipped un-runnable schedule %s (kind=%s not runnable "
                                "this boot) — advanced", sched.get("name"), sched.get("kind"))
                except Exception as exc:
                    logger.warning("could not advance un-runnable schedule %s: %s",
                                   sched.get("name"), exc)
        due = [s for s in due if s["kind"] in runnable_kinds]
        if not due:
            return

    # Silent kinds need no conversation — enqueue with cid=None, independent of sessions.
    for sched in [s for s in due if s["kind"] in _CONVERSATIONLESS_KINDS]:
        try:
            await _enqueue_and_advance(store, sched, None)
        except Exception as exc:
            logger.error("scheduler failed on schedule %s (will retry next tick): %s",
                         sched.get("name"), exc)

    needs_convo = [s for s in due if s["kind"] not in _CONVERSATIONLESS_KINDS]
    if not needs_convo:
        return
    cid = await ensure_reminders_conversation(store, sessions_client)
    if not cid:
        # Nowhere to deliver a reminder — hold off WITHOUT advancing so we retry next
        # tick. (The silent kinds above already ran; they don't need a conversation.)
        logger.warning("%d reminder schedule(s) due but Reminders conversation unavailable — deferring",
                       len(needs_convo))
        return
    for sched in needs_convo:
        try:
            await _enqueue_and_advance(store, sched, cid)
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

    # Only the kinds seeded this boot (their clients are present) are runnable — a due
    # row for any other built-in kind is advanced-but-skipped in _tick, so a stale
    # reminder row can't fire-and-fail after its client was removed.
    runnable_kinds = {d["kind"] for d in defaults}
    logger.info("scheduler started (tick=%ss, schedules=%s)",
                tick_seconds, ",".join(d["name"] for d in defaults))
    while not stop_event.is_set():
        try:
            await _tick(store, sessions_client, runnable_kinds)
        except asyncio.CancelledError:
            raise  # shutdown — propagate so the task ends promptly
        except Exception as exc:
            logger.error("scheduler tick failed (continuing): %s", exc)
        await _wait(stop_event, tick_seconds)
    logger.info("scheduler stopped")
