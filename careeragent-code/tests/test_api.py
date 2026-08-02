"""tests/test_api.py — inbound auth + route wiring (fake workspace, no git/rg)."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.api as api
import security
from safety import CodeProblem

AUTH = {"X-API-Key": "k"}


class FakeWorkspace:
    def sync(self, repo):
        if repo == "bad/..":
            raise CodeProblem(400, "bad repo")
        return {"repo": repo, "head_sha": "sha", "files": 3, "bytes": 42, "cached": False}

    def grep(self, repo, pattern, glob):
        return {"repo": repo, "matches": [{"path": "a.py", "line": 1, "text": "hit"}], "truncated": False}

    def read_file(self, repo, path):
        if path == "missing":
            raise CodeProblem(404, "file not found")
        return {"repo": repo, "path": path, "content": "code", "bytes": 4, "truncated": False}

    def tree(self, repo):
        return {"repo": repo, "entries": [{"path": "a.py", "bytes": 4}], "truncated": False}

    def list_repos(self):
        return [{"repo": "me/repo", "head_sha": "sha", "last_used": 123}]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CODE_API_KEY", "k")
    original = api.workspace
    api.workspace = FakeWorkspace()
    yield TestClient(api.app)   # no context manager → lifespan (real Workspace) not run
    api.workspace = original


# ---- auth ----
class TestAuth:
    @pytest.mark.asyncio
    async def test_missing_key_401(self, monkeypatch):
        monkeypatch.setenv("CODE_API_KEY", "k")
        with pytest.raises(HTTPException) as e:
            await security.verify_api_key(None)
        assert e.value.status_code == 401

    @pytest.mark.asyncio
    async def test_unconfigured_503(self, monkeypatch):
        monkeypatch.delenv("CODE_API_KEY", raising=False)
        with pytest.raises(HTTPException) as e:
            await security.verify_api_key("anything")
        assert e.value.status_code == 503

    def test_sync_requires_auth(self, client):
        assert client.post("/sync", json={"repo": "me/repo"}).status_code == 401


# ---- routes ----
class TestRoutes:
    def test_sync_ok(self, client):
        r = client.post("/sync", headers=AUTH, json={"repo": "me/repo"})
        assert r.status_code == 200
        assert r.json()["head_sha"] == "sha" and r.json()["files"] == 3

    def test_sync_bad_repo_maps_400(self, client):
        r = client.post("/sync", headers=AUTH, json={"repo": "bad/.."})
        assert r.status_code == 400

    def test_grep_ok(self, client):
        r = client.post("/grep", headers=AUTH, json={"repo": "me/repo", "pattern": "hit"})
        assert r.status_code == 200 and r.json()["matches"][0]["text"] == "hit"

    def test_file_ok_and_404(self, client):
        assert client.get("/file", headers=AUTH, params={"repo": "me/repo", "path": "a.py"}).status_code == 200
        assert client.get("/file", headers=AUTH, params={"repo": "me/repo", "path": "missing"}).status_code == 404

    def test_tree_ok(self, client):
        assert client.get("/tree", headers=AUTH, params={"repo": "me/repo"}).status_code == 200

    def test_list_ok(self, client):
        r = client.get("/list", headers=AUTH)
        assert r.status_code == 200 and r.json()[0]["repo"] == "me/repo"

    def test_health_no_auth(self, client):
        r = client.get("/health")
        assert r.status_code == 200 and r.json() == {"status": "ok", "service": "careeragent-code"}
