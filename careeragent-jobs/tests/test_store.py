"""
tests/test_store.py

Two layers:
  1. Pure DB-URL building — always runs, no database touched.
  2. Live round-trips (enqueue/get_job, claim_one, retry_or_fail, finish, list) —
     require a real Postgres and SKIP when none is reachable. Point them at a
     test DB with JOBS_DATABASE_URL (or the JOBS_DB_* parts) to exercise them.
     WARNING: the live tests TRUNCATE the jobs table for isolation, so aim them
     only at a throwaway/test database.
"""
import importlib

import pytest
from sqlalchemy import text


def _reload_store():
    import store
    return importlib.reload(store)


class TestDatabaseUrl:
    def test_builds_asyncpg_url_from_parts(self, monkeypatch):
        monkeypatch.delenv("JOBS_DATABASE_URL", raising=False)
        monkeypatch.setenv("JOBS_DB_USER", "u")
        monkeypatch.setenv("JOBS_DB_PASSWORD", "p")
        monkeypatch.setenv("JOBS_DB_HOST", "h")
        monkeypatch.setenv("JOBS_DB_PORT", "6543")
        monkeypatch.setenv("JOBS_DB_NAME", "db")
        store = _reload_store()
        assert store._database_url() == "postgresql+asyncpg://u:p@h:6543/db"

    def test_explicit_url_wins(self, monkeypatch):
        monkeypatch.setenv("JOBS_DATABASE_URL", "postgresql+asyncpg://x/y")
        store = _reload_store()
        assert store._database_url() == "postgresql+asyncpg://x/y"

    def test_schema_default(self, monkeypatch):
        monkeypatch.delenv("JOBS_DB_SCHEMA", raising=False)
        store = _reload_store()
        assert store.SCHEMA == "careeragent_jobs"


# ---------------------------------------------------------------------------
# Live DB round-trips (skipped when no Postgres is reachable)
# ---------------------------------------------------------------------------
async def _live_store_or_skip():
    """Return a Store against a reachable, freshly-truncated jobs table, or skip."""
    import store as store_mod
    importlib.reload(store_mod)
    st = store_mod.Store()
    if not await st.ping():
        await st.stop()
        pytest.skip("no live Postgres reachable (set JOBS_DATABASE_URL / JOBS_DB_* to run)")
    await st.ensure_schema()
    async with st._engine.begin() as conn:  # isolate: clean slate per test
        await conn.execute(text("TRUNCATE TABLE jobs"))
    return st


class TestLiveStore:
    async def test_enqueue_and_get_job_round_trip(self):
        st = await _live_store_or_skip()
        try:
            created = await st.enqueue(
                "review_repos", {"repos": ["a/b"], "force": True}, None)
            assert created["status"] == "pending"
            job = await st.get_job(created["id"])
            assert job is not None
            assert job["id"] == created["id"]
            assert job["kind"] == "review_repos"
            assert job["spec"] == {"repos": ["a/b"], "force": True}
            assert job["status"] == "pending"
            assert job["attempts"] == 0
            assert job["result"] is None and job["error"] is None
            assert isinstance(job["created_at"], str)
        finally:
            await st.stop()

    async def test_get_job_unknown_is_none(self):
        st = await _live_store_or_skip()
        try:
            assert await st.get_job("00000000-0000-0000-0000-000000000000") is None
        finally:
            await st.stop()

    async def test_claim_one_flips_pending_to_running_then_empty(self):
        st = await _live_store_or_skip()
        try:
            created = await st.enqueue("review_repos", {}, None)
            claimed = await st.claim_one()
            assert claimed is not None
            assert claimed["id"] == created["id"]
            assert claimed["status"] == "running"
            assert claimed["attempts"] == 1
            # No more pending jobs -> None.
            assert await st.claim_one() is None
        finally:
            await st.stop()

    async def test_retry_or_fail_requeues_then_fails_at_cap(self):
        st = await _live_store_or_skip()
        try:
            created = await st.enqueue("review_repos", {}, None)
            job_id = created["id"]
            # Attempt 1
            await st.claim_one()
            await st.retry_or_fail(job_id, "boom-1", max_attempts=3)
            assert (await st.get_job(job_id))["status"] == "pending"
            # Attempt 2
            await st.claim_one()
            await st.retry_or_fail(job_id, "boom-2", max_attempts=3)
            assert (await st.get_job(job_id))["status"] == "pending"
            # Attempt 3 -> cap reached -> failed
            await st.claim_one()
            await st.retry_or_fail(job_id, "boom-3", max_attempts=3)
            final = await st.get_job(job_id)
            assert final["status"] == "failed"
            assert final["error"] == "boom-3"
            assert final["attempts"] == 3
        finally:
            await st.stop()

    async def test_finish_sets_done_with_result(self):
        st = await _live_store_or_skip()
        try:
            created = await st.enqueue("review_repos", {}, None)
            await st.finish(created["id"], "done", result="all good")
            job = await st.get_job(created["id"])
            assert job["status"] == "done"
            assert job["result"] == "all good"
        finally:
            await st.stop()

    async def test_list_jobs_filters_and_newest_first(self):
        st = await _live_store_or_skip()
        try:
            cid = "11111111-1111-1111-1111-111111111111"
            a = await st.enqueue("review_repos", {}, cid)
            b = await st.enqueue("review_repos", {}, cid)
            await st.enqueue("review_repos", {}, None)  # different conversation
            rows = await st.list_jobs(conversation_id=cid, limit=100)
            assert [r["id"] for r in rows] == [b["id"], a["id"]]  # newest first
            # status filter
            await st.finish(a["id"], "done", result="x")
            done = await st.list_jobs(conversation_id=cid, status="done")
            assert [r["id"] for r in done] == [a["id"]]
        finally:
            await st.stop()


async def _live_store_schedules_or_skip():
    """Like _live_store_or_skip but also truncates schedules + jobs_settings for
    the #18b tests (which own those tables)."""
    import store as store_mod
    importlib.reload(store_mod)
    st = store_mod.Store()
    if not await st.ping():
        await st.stop()
        pytest.skip("no live Postgres reachable (set JOBS_DATABASE_URL / JOBS_DB_* to run)")
    await st.ensure_schema()
    async with st._engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE schedules"))
        await conn.execute(text("TRUNCATE TABLE jobs_settings"))
    return st


class TestLiveSchedules:
    async def test_seed_is_idempotent(self):
        st = await _live_store_schedules_or_skip()
        try:
            defs = [{"name": "follow_up_scan", "kind": "follow_up_scan",
                     "spec": {}, "interval_seconds": 86400}]
            await st.seed_default_schedules(defs)
            await st.seed_default_schedules(defs)   # second seed must NOT duplicate
            rows = await st.list_schedules()
            assert [r["name"] for r in rows] == ["follow_up_scan"]
            assert rows[0]["interval_seconds"] == 86400
            assert rows[0]["enabled"] is True
        finally:
            await st.stop()

    async def test_due_schedules_and_advance(self):
        st = await _live_store_schedules_or_skip()
        try:
            # A schedule seeded with next_run defaulting to now() is immediately due.
            await st.seed_default_schedules(
                [{"name": "follow_up_scan", "kind": "follow_up_scan",
                  "spec": {"k": "v"}, "interval_seconds": 3600}])
            due = await st.due_schedules()
            assert [d["name"] for d in due] == ["follow_up_scan"]
            assert due[0]["spec"] == {"k": "v"}
            # Advancing pushes next_run into the future -> no longer due.
            await st.advance_schedule(due[0]["id"], due[0]["interval_seconds"])
            assert await st.due_schedules() == []
            # last_run is now set.
            rows = await st.list_schedules()
            assert rows[0]["last_run"] is not None
        finally:
            await st.stop()

    async def test_settings_round_trip_and_upsert(self):
        st = await _live_store_schedules_or_skip()
        try:
            assert await st.get_setting("reminders_conversation_id") is None
            await st.set_setting("reminders_conversation_id", "cid-1")
            assert await st.get_setting("reminders_conversation_id") == "cid-1"
            await st.set_setting("reminders_conversation_id", "cid-2")  # upsert
            assert await st.get_setting("reminders_conversation_id") == "cid-2"
        finally:
            await st.stop()
