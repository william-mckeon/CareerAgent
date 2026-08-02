"""
tests/test_harness.py — the orchestrator's partition / idempotency / fan-out /
reduce, with fake infra + mcp + dossier. No network.
"""
import json

from harness.orchestrator import Orchestrator
from schemas import ReviewRequest


class FakeMCP:
    prefix = "mcp__github__"
    started = True

    def __init__(self, head=None):
        self._head = head

    def schemas(self):
        return []

    def owns(self, name):
        return name.startswith(self.prefix)

    async def call(self, name, args, max_chars=None):
        if name.endswith("list_commits"):
            return (True, json.dumps([{"sha": self._head}])) if self._head else (False, "x")
        return False, "x"


class FakeInfra:
    """Always submits a minimal review immediately."""
    async def complete(self, payload):
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "1", "type": "function",
             "function": {"name": "submit_review", "arguments": json.dumps({"name": "r", "summary": "s"})}}]}}]}


class FakeDossier:
    def __init__(self, existing=None):
        self._existing = existing
        self.saved = []

    async def get_by_external_id(self, ext):
        return self._existing

    async def save_project(self, fields):
        self.saved.append(fields)
        return 201, {"id": "p1", "upserted": False}


def _orch(mcp, dossier, **kw):
    return Orchestrator(infra=FakeInfra(), mcp=mcp, dossier=dossier, **kw)


async def test_reviews_explicit_repos_and_writes_github_source():
    dossier = FakeDossier()
    orch = _orch(FakeMCP(head="abc"), dossier)
    resp = await orch.review_batch(ReviewRequest(repos=["me/a", "me/b"]))
    assert (resp.reviewed, resp.skipped, resp.errors) == (2, 0, 0)
    assert all(w["source"] == "github" and w["commit_sha"] == "abc" for w in dossier.saved)
    assert {w["external_id"] for w in dossier.saved} == {"me/a", "me/b"}


async def test_skips_unchanged_repo():
    dossier = FakeDossier(existing={"commit_sha": "abc", "id": "p1"})
    orch = _orch(FakeMCP(head="abc"), dossier)
    resp = await orch.review_batch(ReviewRequest(repos=["me/a"]))
    assert (resp.reviewed, resp.skipped) == (0, 1)
    assert dossier.saved == []                      # skipped → no write
    assert resp.outcomes[0].detail == "unchanged"


async def test_force_reviews_even_if_unchanged():
    dossier = FakeDossier(existing={"commit_sha": "abc", "id": "p1"})
    orch = _orch(FakeMCP(head="abc"), dossier)
    resp = await orch.review_batch(ReviewRequest(repos=["me/a"], force=True))
    assert resp.reviewed == 1 and dossier.saved


async def test_dedup_and_cap():
    orch = _orch(FakeMCP(head="abc"), FakeDossier(), max_repos=2)
    resp = await orch.review_batch(ReviewRequest(repos=["me/a", "me/a", "me/b", "me/c"]))
    assert len(resp.outcomes) == 2                  # 'me/a' de-duped, capped at 2


async def test_no_head_sha_fails_open_and_reviews():
    dossier = FakeDossier(existing={"commit_sha": "abc"})  # would match, but head is None
    orch = _orch(FakeMCP(head=None), dossier)
    resp = await orch.review_batch(ReviewRequest(repos=["me/a"]))
    assert resp.reviewed == 1                        # no head → can't skip → review


async def test_no_repos_yields_error_outcome():
    orch = _orch(FakeMCP(head=None), FakeDossier())
    resp = await orch.review_batch(ReviewRequest(repos=[]))
    assert resp.errors == 1 and resp.reviewed == 0
