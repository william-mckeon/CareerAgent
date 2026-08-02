"""
tests/test_api.py — the HTTP surface (auth, validation, shapes).

Uses Starlette's TestClient WITHOUT the context manager, so the lifespan (and its
worker + real DB/clients) never runs; the module-level `store` is replaced with a
fake. This mirrors the hermetic style of the sibling services' tests — no DB, no
network — while still exercising the endpoints end-to-end.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import api as api_module
from security import verify_api_key

from conftest import TEST_API_KEY

AUTH = {"X-API-Key": TEST_API_KEY}


def _sample_job(**over):
    job = {
        "id": "22222222-2222-2222-2222-222222222222",
        "kind": "review_repos",
        "status": "done",
        "attempts": 1,
        "result": "done summary",
        "error": None,
        "conversation_id": "33333333-3333-3333-3333-333333333333",
        "created_at": "2026-07-21T00:00:00+00:00",
        "updated_at": "2026-07-21T00:00:01+00:00",
    }
    job.update(over)
    return job


class FakeStore:
    def __init__(self):
        self.enqueued = []
        self.get_result = None
        self.list_result = []
        self.schedules_result = []
        self.ping_result = True

    async def enqueue(self, kind, spec, conversation_id):
        self.enqueued.append((kind, spec, conversation_id))
        return {"id": "11111111-1111-1111-1111-111111111111", "status": "pending"}

    async def get_job(self, job_id):
        return self.get_result

    async def list_jobs(self, conversation_id=None, status=None, limit=100):
        self.list_result_args = (conversation_id, status, limit)
        return self.list_result

    async def list_schedules(self):
        return self.schedules_result

    async def ping(self):
        return self.ping_result


@pytest.fixture
def client():
    return TestClient(api_module.app)  # no context manager -> lifespan does NOT run


@pytest.fixture
def fake_store():
    original = api_module.store
    st = FakeStore()
    api_module.store = st
    yield st
    api_module.store = original


class _FakeRunningTask:
    """Stand-in for a live worker task: POST /jobs rejects (503) unless one is running."""
    def done(self):
        return False


@pytest.fixture
def worker_up():
    original = api_module._worker_task
    api_module._worker_task = _FakeRunningTask()
    yield
    api_module._worker_task = original


# --------------------------------------------------------------------------- auth
class TestVerifyApiKey:
    @pytest.mark.asyncio
    async def test_correct_key_passes(self):
        assert await verify_api_key(key=TEST_API_KEY) == TEST_API_KEY

    @pytest.mark.asyncio
    async def test_wrong_key_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            await verify_api_key(key="nope")
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_key_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            await verify_api_key(key=None)
        assert exc.value.status_code == 401


# --------------------------------------------------------------------- POST /jobs
class TestCreateJob:
    def test_valid_returns_201_pending(self, client, fake_store, worker_up):
        r = client.post("/jobs", headers=AUTH,
                        json={"kind": "review_repos", "spec": {"limit": 5},
                              "conversation_id": "33333333-3333-3333-3333-333333333333"})
        assert r.status_code == 201
        assert r.json() == {"id": "11111111-1111-1111-1111-111111111111", "status": "pending"}
        assert fake_store.enqueued == [
            ("review_repos", {"limit": 5}, "33333333-3333-3333-3333-333333333333")]

    def test_defaults_spec_and_null_conversation(self, client, fake_store, worker_up):
        r = client.post("/jobs", headers=AUTH, json={"kind": "review_repos"})
        assert r.status_code == 201
        assert fake_store.enqueued == [("review_repos", {}, None)]

    def test_worker_down_returns_503(self, client, fake_store):
        # No running worker -> jobs would sit 'pending' forever, so reject them.
        original = api_module._worker_task
        api_module._worker_task = None
        try:
            r = client.post("/jobs", headers=AUTH, json={"kind": "review_repos"})
            assert r.status_code == 503
        finally:
            api_module._worker_task = original

    def test_unknown_kind_400(self, client, fake_store):
        r = client.post("/jobs", headers=AUTH, json={"kind": "make_coffee", "spec": {}})
        assert r.status_code == 400
        assert r.json() == {"detail": "unknown job kind 'make_coffee'"}

    def test_bad_conversation_uuid_400(self, client, fake_store):
        r = client.post("/jobs", headers=AUTH,
                        json={"kind": "review_repos", "conversation_id": "not-a-uuid"})
        assert r.status_code == 400

    def test_missing_key_401(self, client, fake_store):
        r = client.post("/jobs", json={"kind": "review_repos"})
        assert r.status_code == 401

    def test_wrong_key_401(self, client, fake_store):
        r = client.post("/jobs", headers={"X-API-Key": "wrong"},
                        json={"kind": "review_repos"})
        assert r.status_code == 401

    def test_store_unavailable_503(self, client, worker_up):
        original = api_module.store
        api_module.store = None
        try:
            r = client.post("/jobs", headers=AUTH, json={"kind": "review_repos"})
            assert r.status_code == 503
        finally:
            api_module.store = original


# ------------------------------------------------------------------ GET /jobs/{id}
class TestGetJob:
    def test_found_returns_public_shape(self, client, fake_store):
        fake_store.get_result = _sample_job()
        r = client.get("/jobs/22222222-2222-2222-2222-222222222222", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"id", "kind", "status", "attempts", "result", "error",
                             "conversation_id", "created_at", "updated_at"}
        assert "spec" not in body           # spec is worker-internal, never exposed
        assert body["status"] == "done"

    def test_unknown_404(self, client, fake_store):
        fake_store.get_result = None
        r = client.get("/jobs/22222222-2222-2222-2222-222222222222", headers=AUTH)
        assert r.status_code == 404

    def test_malformed_id_404(self, client, fake_store):
        r = client.get("/jobs/not-a-uuid", headers=AUTH)
        assert r.status_code == 404

    def test_requires_auth(self, client, fake_store):
        r = client.get("/jobs/22222222-2222-2222-2222-222222222222")
        assert r.status_code == 401


# ---------------------------------------------------------------------- GET /jobs
class TestListJobs:
    def test_returns_list_and_passes_filters(self, client, fake_store):
        fake_store.list_result = [_sample_job(), _sample_job(id="x", status="pending")]
        r = client.get("/jobs?conversation_id=33333333-3333-3333-3333-333333333333"
                       "&status=done&limit=10", headers=AUTH)
        assert r.status_code == 200
        assert len(r.json()) == 2
        assert fake_store.list_result_args == (
            "33333333-3333-3333-3333-333333333333", "done", 10)

    def test_limit_capped_at_100(self, client, fake_store):
        fake_store.list_result = []
        r = client.get("/jobs?limit=9999", headers=AUTH)
        assert r.status_code == 200
        assert fake_store.list_result_args == (None, None, 100)  # clamped to the cap

    def test_requires_auth(self, client, fake_store):
        r = client.get("/jobs")
        assert r.status_code == 401


# ----------------------------------------------------------------- GET /schedules
class TestListSchedules:
    def test_returns_schedules(self, client, fake_store):
        fake_store.schedules_result = [
            {"id": "s1", "name": "follow_up_scan", "kind": "follow_up_scan",
             "interval_seconds": 86400, "enabled": True,
             "next_run": "2026-07-22T00:00:00+00:00", "last_run": None,
             "created_at": "2026-07-21T00:00:00+00:00"},
        ]
        r = client.get("/schedules", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["name"] == "follow_up_scan"

    def test_requires_auth(self, client, fake_store):
        assert client.get("/schedules").status_code == 401


# --------------------------------------------------------------------- GET /health
class TestHealth:
    def test_ok_when_db_reachable(self, client, fake_store):
        fake_store.ping_result = True
        r = client.get("/health")
        assert r.status_code == 200
        # worker/scheduler are 'stopped' here: TestClient skips the lifespan, so no
        # background task is set on the module.
        assert r.json() == {"status": "ok", "service": "careeragent-jobs", "database": "ok",
                            "worker": "stopped", "scheduler": "stopped"}

    def test_degraded_when_db_down(self, client, fake_store):
        fake_store.ping_result = False
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "degraded", "service": "careeragent-jobs",
                            "database": "unreachable", "worker": "stopped", "scheduler": "stopped"}

    def test_no_auth_required(self, client, fake_store):
        assert client.get("/health").status_code == 200
