"""
tests/test_scheduler.py — the recurring-job scheduler (pure; in-memory fakes).

Covers the branches the scheduler owns without a DB or network:
  * ensure_reminders_conversation — reuse a live id, create when absent, recreate
    on a 404 (user deleted it), reuse on a transient error, defer (None) when
    creation fails.
  * _tick — enqueue + advance each due schedule; defer (no advance) when the
    Reminders conversation can't be resolved; no-op when nothing is due.
  * run_scheduler_loop — seeds once, then ticks and stops cleanly on the event.
"""
import asyncio

import pytest

from scheduler import (
    REMINDERS_CONVERSATION_KEY,
    REMINDERS_TITLE,
    default_schedules,
    ensure_reminders_conversation,
    run_scheduler_loop,
    _tick,
)


class FakeStore:
    """In-memory settings + a scripted due-schedule queue; records enqueue/advance."""

    def __init__(self, due=None, settings=None):
        self._due = list(due or [])
        self._settings = dict(settings or {})
        self.enqueued = []          # (kind, spec, conversation_id)
        self.advanced = []          # (schedule_id, interval_seconds)
        self.seeded = None

    async def get_setting(self, key):
        return self._settings.get(key)

    async def set_setting(self, key, value):
        self._settings[key] = value

    async def due_schedules(self):
        return list(self._due)

    async def advance_schedule(self, schedule_id, interval_seconds):
        self.advanced.append((schedule_id, interval_seconds))

    async def enqueue(self, kind, spec, conversation_id):
        self.enqueued.append((kind, spec, conversation_id))
        return {"id": f"job-{len(self.enqueued)}", "status": "pending"}

    async def seed_default_schedules(self, defaults):
        self.seeded = defaults


class FakeSessions:
    """Scriptable get_conversation / create_conversation / list_conversations."""

    def __init__(self, get_status=200, create=(200, {"conversation_id": "new-cid"}),
                 conversations=(200, [])):
        self._get_status = get_status
        self._create = create
        self._conversations = conversations
        self.created = 0
        self.get_calls = []
        self.list_calls = 0
        self._last_title = None

    async def get_conversation(self, conversation_id):
        self.get_calls.append(conversation_id)
        return self._get_status, {"conversation_id": conversation_id}

    async def create_conversation(self, title):
        self.created += 1
        self._last_title = title
        return self._create

    async def list_conversations(self, limit=200):
        self.list_calls += 1
        return self._conversations


class TestEnsureRemindersConversation:
    async def test_reuses_existing_live_conversation(self):
        store = FakeStore(settings={REMINDERS_CONVERSATION_KEY: "cid-1"})
        sessions = FakeSessions(get_status=200)
        cid = await ensure_reminders_conversation(store, sessions)
        assert cid == "cid-1"
        assert sessions.created == 0                # did NOT create a new one
        assert sessions.get_calls == ["cid-1"]

    async def test_creates_when_none_on_file(self):
        store = FakeStore(settings={})
        sessions = FakeSessions(create=(200, {"conversation_id": "fresh"}))
        cid = await ensure_reminders_conversation(store, sessions)
        assert cid == "fresh"
        assert sessions.created == 1
        assert sessions._last_title == REMINDERS_TITLE
        # persisted for next time
        assert store._settings[REMINDERS_CONVERSATION_KEY] == "fresh"

    async def test_recreates_when_persisted_one_is_404(self):
        # The user deleted the reminders conversation -> mint a new one.
        store = FakeStore(settings={REMINDERS_CONVERSATION_KEY: "gone"})
        sessions = FakeSessions(get_status=404, create=(200, {"conversation_id": "brand-new"}))
        cid = await ensure_reminders_conversation(store, sessions)
        assert cid == "brand-new"
        assert sessions.created == 1
        assert store._settings[REMINDERS_CONVERSATION_KEY] == "brand-new"

    async def test_reuses_on_transient_verify_error(self):
        # A 5xx/0 while verifying must NOT fork a new conversation.
        store = FakeStore(settings={REMINDERS_CONVERSATION_KEY: "cid-2"})
        sessions = FakeSessions(get_status=503)
        cid = await ensure_reminders_conversation(store, sessions)
        assert cid == "cid-2"
        assert sessions.created == 0

    async def test_returns_none_when_create_fails(self):
        store = FakeStore(settings={})
        # No title match to adopt -> must try create, which fails.
        sessions = FakeSessions(conversations=(200, []), create=(0, {"error": "refused"}))
        cid = await ensure_reminders_conversation(store, sessions)
        assert cid is None
        assert REMINDERS_CONVERSATION_KEY not in store._settings

    async def test_adopts_existing_by_title_instead_of_forking(self):
        # Bootstrap: no persisted id, but sessions ALREADY has a "🔔 Reminders"
        # thread (e.g. a prior create whose response was lost). Adopt it, do NOT
        # mint a second one — this is the self-healing dedup.
        store = FakeStore(settings={})
        sessions = FakeSessions(conversations=(200, [
            {"conversation_id": "other", "title": "Chat", "created_at": "2026-07-01T00:00:00+00:00"},
            {"conversation_id": "reminders-A", "title": REMINDERS_TITLE,
             "created_at": "2026-07-20T00:00:00+00:00"},
        ]))
        cid = await ensure_reminders_conversation(store, sessions)
        assert cid == "reminders-A"
        assert sessions.created == 0                       # did NOT fork a new thread
        assert store._settings[REMINDERS_CONVERSATION_KEY] == "reminders-A"

    async def test_reconcile_picks_oldest_match(self):
        # Two orphans -> deterministically converge on the OLDEST (stable).
        store = FakeStore(settings={})
        sessions = FakeSessions(conversations=(200, [
            {"conversation_id": "newer", "title": REMINDERS_TITLE,
             "created_at": "2026-07-20T00:00:00+00:00"},
            {"conversation_id": "older", "title": REMINDERS_TITLE,
             "created_at": "2026-07-05T00:00:00+00:00"},
        ]))
        cid = await ensure_reminders_conversation(store, sessions)
        assert cid == "older"
        assert sessions.created == 0

    async def test_persist_failure_does_not_propagate(self):
        # set_setting failing (read-only DB / write outage) must NOT raise out of
        # ensure_reminders_conversation — the created thread is returned anyway and
        # re-adopted by title next tick.
        class RaisingStore(FakeStore):
            async def set_setting(self, key, value):
                raise RuntimeError("db is read-only")
        store = RaisingStore(settings={})
        sessions = FakeSessions(conversations=(200, []),
                                create=(200, {"conversation_id": "fresh"}))
        cid = await ensure_reminders_conversation(store, sessions)
        assert cid == "fresh"                              # returned despite persist failure
        assert sessions.created == 1


class TestTick:
    async def test_enqueues_and_advances_each_due(self):
        due = [
            {"id": "s1", "name": "follow_up_scan", "kind": "follow_up_scan",
             "spec": {}, "interval_seconds": 86400},
            {"id": "s2", "name": "resume_freshness", "kind": "resume_freshness",
             "spec": {"x": 1}, "interval_seconds": 3600},
        ]
        store = FakeStore(due=due, settings={REMINDERS_CONVERSATION_KEY: "cid"})
        sessions = FakeSessions(get_status=200)
        await _tick(store, sessions)
        assert store.enqueued == [
            ("follow_up_scan", {}, "cid"),
            ("resume_freshness", {"x": 1}, "cid"),
        ]
        assert store.advanced == [("s1", 86400), ("s2", 3600)]

    async def test_defers_without_advancing_when_no_conversation(self):
        due = [{"id": "s1", "name": "follow_up_scan", "kind": "follow_up_scan",
                "spec": {}, "interval_seconds": 86400}]
        store = FakeStore(due=due, settings={})
        sessions = FakeSessions(create=(0, {"error": "sessions down"}))  # can't resolve cid
        await _tick(store, sessions)
        assert store.enqueued == []      # nothing enqueued
        assert store.advanced == []      # NOT advanced -> retried next tick

    async def test_noop_when_nothing_due(self):
        store = FakeStore(due=[], settings={REMINDERS_CONVERSATION_KEY: "cid"})
        sessions = FakeSessions()
        await _tick(store, sessions)
        assert store.enqueued == []
        assert store.advanced == []
        assert sessions.created == 0     # didn't even resolve a conversation

    async def test_silent_kind_enqueued_without_a_conversation(self):
        # Slice E — repo_presync never injects, so it is enqueued with conversation_id
        # None and does NOT resolve/mint the Reminders conversation.
        due = [{"id": "s3", "name": "repo_presync", "kind": "repo_presync",
                "spec": {}, "interval_seconds": 86400}]
        store = FakeStore(due=due, settings={})
        sessions = FakeSessions()
        await _tick(store, sessions)
        assert store.enqueued == [("repo_presync", {}, None)]
        assert store.advanced == [("s3", 86400)]
        assert sessions.created == 0 and sessions.list_calls == 0   # no Reminders thread touched

    async def test_silent_warm_runs_even_when_sessions_is_down(self):
        # A sessions outage can't defer the cache-warm — it needs no conversation.
        due = [{"id": "s3", "name": "repo_presync", "kind": "repo_presync",
                "spec": {}, "interval_seconds": 86400}]
        store = FakeStore(due=due, settings={})
        sessions = FakeSessions(create=(0, {"error": "sessions down"}))
        await _tick(store, sessions)
        assert store.enqueued == [("repo_presync", {}, None)]   # still enqueued
        assert store.advanced == [("s3", 86400)]

    async def test_mixed_silent_and_reminder(self):
        due = [
            {"id": "s1", "name": "follow_up_scan", "kind": "follow_up_scan",
             "spec": {}, "interval_seconds": 86400},
            {"id": "s3", "name": "repo_presync", "kind": "repo_presync",
             "spec": {}, "interval_seconds": 3600},
        ]
        store = FakeStore(due=due, settings={REMINDERS_CONVERSATION_KEY: "cid"})
        sessions = FakeSessions(get_status=200)
        await _tick(store, sessions)
        # reminder → the Reminders cid; silent warm → None
        assert ("follow_up_scan", {}, "cid") in store.enqueued
        assert ("repo_presync", {}, None) in store.enqueued
        assert set(store.advanced) == {("s1", 86400), ("s3", 3600)}

    async def test_unrunnable_kind_is_advanced_but_not_enqueued(self):
        # Finding-3 fix: a stale reminder row whose client is absent this boot must be
        # advanced (so it doesn't re-fire immediately) but NOT enqueued (no fire-and-fail).
        due = [
            {"id": "s1", "name": "follow_up_scan", "kind": "follow_up_scan",
             "spec": {}, "interval_seconds": 86400},   # NOT runnable this boot
            {"id": "s3", "name": "repo_presync", "kind": "repo_presync",
             "spec": {}, "interval_seconds": 3600},    # runnable
        ]
        store = FakeStore(due=due, settings={})
        sessions = FakeSessions()
        await _tick(store, sessions, runnable_kinds={"repo_presync"})
        assert store.enqueued == [("repo_presync", {}, None)]   # only the runnable one ran
        assert set(store.advanced) == {("s1", 86400), ("s3", 3600)}  # both advanced
        assert sessions.created == 0


class TestDefaultSchedules:
    def test_builds_two_reminder_schedules(self):
        defs = default_schedules(111, 222)
        by_name = {d["name"]: d for d in defs}
        assert set(by_name) == {"follow_up_scan", "resume_freshness"}
        assert by_name["follow_up_scan"]["kind"] == "follow_up_scan"
        assert by_name["follow_up_scan"]["interval_seconds"] == 111
        assert by_name["resume_freshness"]["interval_seconds"] == 222

    def test_seeds_repo_presync_when_interval_given(self):
        # Slice E — a positive presync interval seeds the third (repo_presync) schedule.
        defs = default_schedules(111, 222, 333)
        by_name = {d["name"]: d for d in defs}
        assert set(by_name) == {"follow_up_scan", "resume_freshness", "repo_presync"}
        assert by_name["repo_presync"]["kind"] == "repo_presync"
        assert by_name["repo_presync"]["interval_seconds"] == 333
        assert by_name["repo_presync"]["spec"] == {}

    def test_zero_interval_skips_that_kind(self):
        # 0 means "client absent → do not seed" — the gate passes 0 for a kind whose
        # client is missing, so no schedule is seeded that would only fail.
        only_presync = default_schedules(0, 0, 333)
        assert {d["name"] for d in only_presync} == {"repo_presync"}
        no_presync = default_schedules(111, 222, 0)
        assert {d["name"] for d in no_presync} == {"follow_up_scan", "resume_freshness"}
        assert default_schedules(0, 0, 0) == []


class TestRunSchedulerLoop:
    async def test_seeds_then_ticks_then_stops(self):
        due = [{"id": "s1", "name": "follow_up_scan", "kind": "follow_up_scan",
                "spec": {}, "interval_seconds": 86400}]
        store = FakeStore(due=due, settings={REMINDERS_CONVERSATION_KEY: "cid"})
        sessions = FakeSessions(get_status=200)
        stop = asyncio.Event()
        defaults = default_schedules(10, 20)

        async def run():
            await run_scheduler_loop(store, sessions, defaults,
                                     tick_seconds=0.01, stop_event=stop)

        task = asyncio.create_task(run())
        for _ in range(50):
            if store.enqueued:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert store.seeded == defaults           # seeded once at startup
        assert ("follow_up_scan", {}, "cid") in store.enqueued

    async def test_stops_promptly_when_event_preset(self):
        store = FakeStore(due=[], settings={})
        sessions = FakeSessions()
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(
            run_scheduler_loop(store, sessions, default_schedules(1, 1),
                               tick_seconds=5.0, stop_event=stop),
            timeout=1.0,
        )
        assert store.seeded is not None           # seeding happens before the loop guard
