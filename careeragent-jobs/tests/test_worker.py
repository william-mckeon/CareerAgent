"""
tests/test_worker.py — the worker's per-job logic + loop lifecycle (no DB/network).

Uses in-memory fakes for the store, the sessions client, and the handlers to
verify the branches the worker owns: success -> finish('done') + inject; handler
error -> retry_or_fail; unknown kind -> immediate fail; and that the loop drains
the queue then idles/stops cleanly on the stop_event.
"""
import asyncio

import pytest

from worker import execute_job, run_worker_loop


class FakeStore:
    """Records finish/retry_or_fail and serves a scripted claim_one queue."""

    def __init__(self, queue=None):
        self._queue = list(queue or [])
        self.finished = []          # (job_id, status, result, error)
        self.retried = []           # (job_id, error, max_attempts)
        self.claim_calls = 0

    async def claim_one(self):
        self.claim_calls += 1
        return self._queue.pop(0) if self._queue else None

    async def finish(self, job_id, status, result=None, error=None):
        self.finished.append((job_id, status, result, error))

    async def retry_or_fail(self, job_id, error, max_attempts):
        self.retried.append((job_id, error, max_attempts))


class FakeSessions:
    """Records inject calls; returns a configurable status."""

    def __init__(self, status=200):
        self._status = status
        self.injected = []

    async def inject(self, conversation_id, role, content):
        self.injected.append((conversation_id, role, content))
        return self._status, {"ok": True}


async def _ok_handler(spec, deps):
    return "SUMMARY"


async def _empty_handler(spec, deps):
    return ""   # "ran fine, nothing to report" — worker must skip the inject


async def _boom_handler(spec, deps):
    raise RuntimeError("handler blew up")


class TestExecuteJob:
    async def test_success_finishes_done_and_injects(self):
        store = FakeStore()
        sessions = FakeSessions()
        job = {"id": "j1", "kind": "k", "spec": {}, "conversation_id": "c1"}
        await execute_job(job, store, {"k": _ok_handler}, deps=None,
                          sessions_client=sessions, max_attempts=3)
        assert store.finished == [("j1", "done", "SUMMARY", None)]
        assert sessions.injected == [("c1", "assistant", "SUMMARY")]
        assert store.retried == []

    async def test_success_without_conversation_does_not_inject(self):
        store = FakeStore()
        sessions = FakeSessions()
        job = {"id": "j2", "kind": "k", "spec": {}, "conversation_id": None}
        await execute_job(job, store, {"k": _ok_handler}, deps=None,
                          sessions_client=sessions, max_attempts=3)
        assert store.finished == [("j2", "done", "SUMMARY", None)]
        assert sessions.injected == []

    async def test_empty_result_finishes_done_but_skips_inject(self):
        # #18b: a reminder scan with nothing due returns "" — job is still 'done'
        # but the empty result must NOT be injected into the conversation.
        store = FakeStore()
        sessions = FakeSessions()
        job = {"id": "j6", "kind": "k", "spec": {}, "conversation_id": "c1"}
        await execute_job(job, store, {"k": _empty_handler}, deps=None,
                          sessions_client=sessions, max_attempts=3)
        assert store.finished == [("j6", "done", "", None)]
        assert sessions.injected == []      # nothing injected for an empty result

    async def test_handler_error_retries(self):
        store = FakeStore()
        sessions = FakeSessions()
        job = {"id": "j3", "kind": "k", "spec": {}, "conversation_id": "c1"}
        await execute_job(job, store, {"k": _boom_handler}, deps=None,
                          sessions_client=sessions, max_attempts=3)
        assert store.retried == [("j3", "handler blew up", 3)]
        assert store.finished == []       # not finished
        assert sessions.injected == []    # nothing injected on failure

    async def test_unknown_kind_fails_immediately(self):
        store = FakeStore()
        sessions = FakeSessions()
        job = {"id": "j4", "kind": "nope", "spec": {}, "conversation_id": "c1"}
        await execute_job(job, store, {"k": _ok_handler}, deps=None,
                          sessions_client=sessions, max_attempts=3)
        assert store.finished[0][:2] == ("j4", "failed")
        assert store.retried == []

    async def test_inject_failure_retries_then_still_marks_done(self, monkeypatch):
        # A transient sessions failure is RETRIED a few times; the job stays 'done'
        # (result durably stored, recoverable via GET /jobs) even if inject never lands.
        import worker as worker_mod
        async def _no_sleep(*a, **k):
            return None
        monkeypatch.setattr(worker_mod.asyncio, "sleep", _no_sleep)   # keep the test fast
        store = FakeStore()
        sessions = FakeSessions(status=404)  # inject keeps failing
        job = {"id": "j5", "kind": "k", "spec": {}, "conversation_id": "c1"}
        await execute_job(job, store, {"k": _ok_handler}, deps=None,
                          sessions_client=sessions, max_attempts=3)
        assert store.finished == [("j5", "done", "SUMMARY", None)]     # result stored
        assert len(sessions.injected) == 3                            # retried the transient failure


class TestRunWorkerLoop:
    async def test_drains_queue_then_stops(self):
        store = FakeStore(queue=[
            {"id": "a", "kind": "k", "spec": {}, "conversation_id": None},
            {"id": "b", "kind": "k", "spec": {}, "conversation_id": None},
        ])
        sessions = FakeSessions()
        stop = asyncio.Event()

        async def run():
            await run_worker_loop(store, {"k": _ok_handler}, None, sessions,
                                  poll_seconds=0.01, max_attempts=3, stop_event=stop)

        task = asyncio.create_task(run())
        # Give the loop a few ticks to drain both jobs, then ask it to stop.
        for _ in range(50):
            if len(store.finished) >= 2:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert {f[0] for f in store.finished} == {"a", "b"}

    async def test_stops_promptly_on_empty_queue(self):
        store = FakeStore(queue=[])
        sessions = FakeSessions()
        stop = asyncio.Event()
        stop.set()  # already asked to stop
        # With the event set, the loop must return without hanging on the poll sleep.
        await asyncio.wait_for(
            run_worker_loop(store, {"k": _ok_handler}, None, sessions,
                            poll_seconds=5.0, max_attempts=3, stop_event=stop),
            timeout=1.0,
        )
