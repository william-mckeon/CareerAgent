"""
tests/test_background_jobs.py — Phase 7 #18a: the spawn_job control tool.

spawn_job starts a SLOW task in the background: it enqueues a job to
careeragent-jobs with the current conversation_id and returns immediately (the
worker runs it and injects the result later — "do not poll"). It is control-
intercepted, gated to edit modes, and fail-soft when careeragent-jobs is absent.
See careeragent-api/specs/0012-background-jobs.md.
"""
import json

from agent import loop as agent_loop
from agent import permissions, tools


class RecordingInfra:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def complete(self, payload):
        r = self._responses[self.calls]
        self.calls += 1
        return r


class FakeDossier:
    async def read_profile(self):
        return 200, {"content": "# Profile", "version": 1}

    async def search_projects(self, params):
        return 200, []

    async def list_preferences(self):
        return 200, []


class FakeJobs:
    """Duck-typed stand-in for JobsClient — records the (kind, spec, conversation_id)."""
    def __init__(self, status=201, body=None):
        self.calls = []
        self._status = status
        self._body = body if body is not None else {"id": "job-1", "status": "pending"}

    async def enqueue(self, kind, spec, conversation_id):
        self.calls.append((kind, spec, conversation_id))
        return self._status, self._body


def _tc(name, args):
    return {"id": f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _completion(content="", tool_calls=None):
    m = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return {"choices": [{"message": m}]}


async def _drain(gen):
    out = b""
    async for chunk in gen:
        out += chunk
    return out


def _content(out: str) -> str:
    parts = []
    for line in out.splitlines():
        if line.startswith("data:"):
            p = line[5:].strip()
            if p.startswith("{"):
                try:
                    d = ((json.loads(p).get("choices") or [{}])[0]).get("delta") or {}
                except Exception:
                    continue
                if isinstance(d.get("content"), str):
                    parts.append(d["content"])
    return "".join(parts)


def _run(infra, *, mode="acceptEdits", jobs_client=None, conversation_id="c-1"):
    return agent_loop.run_agent(
        messages=[{"role": "user", "content": "review all my github repos"}], mode=mode,
        persona="CareerAgent.", infra_client=infra, dossier_client=FakeDossier(), max_steps=40,
        guardian_enabled=False, jobs_client=jobs_client, conversation_id=conversation_id)


def test_spawn_job_is_a_non_mutating_control_tool_in_every_mode():
    assert "spawn_job" in tools.CONTROL_TOOLS
    assert not permissions.is_mutating("spawn_job")
    for mode in ("plan", "default", "acceptEdits", "bypass"):
        names = {s["function"]["name"] for s in tools.schemas_for_mode(mode)}
        assert "spawn_job" in names


async def test_spawn_job_enqueues_with_conversation_and_finishes():
    jobs = FakeJobs()
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("spawn_job", {"kind": "review_repos", "spec": {"limit": 5}})]),
        _completion(tool_calls=[_tc("finish_answer",
                    {"summary": "I'm reviewing your repos in the background — I'll post the results here."})]),
    ])
    out = (await _drain(_run(infra, jobs_client=jobs))).decode("utf-8")
    # enqueued with the CURRENT conversation_id so the result injects back
    assert jobs.calls == [("review_repos", {"limit": 5}, "c-1")]
    assert "background" in _content(out)
    assert "job-1" in out                      # the job id surfaced on the reasoning channel


async def test_spawn_job_blocked_in_plan_mode():
    jobs = FakeJobs()
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("spawn_job", {"kind": "review_repos"})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "I can't start that in plan mode."})]),
    ])
    out = (await _drain(_run(infra, mode="plan", jobs_client=jobs))).decode("utf-8")
    assert jobs.calls == []                     # never enqueued (plan hard-denies the write)
    assert "spawn_job blocked" in out


async def test_spawn_job_blocked_in_default_mode_needs_approval():
    # In 'default' mode the underlying review_repos write NEEDS approval, so a
    # background job (which can't pause) is refused — it must not bypass the gate.
    jobs = FakeJobs()
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("spawn_job", {"kind": "review_repos"})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "I'll do it inline so you can confirm."})]),
    ])
    out = (await _drain(_run(infra, mode="default", jobs_client=jobs))).decode("utf-8")
    assert jobs.calls == []                     # gated by permissions.decide, not just != plan
    assert "spawn_job blocked" in out


async def test_spawn_job_coerces_stringified_spec():
    # gpt-oss often emits `spec` as a JSON STRING; it must be parsed, not dropped to {}.
    jobs = FakeJobs()
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("spawn_job",
                    {"kind": "review_repos", "spec": '{"repos": ["octocat/hello"], "limit": 2}'})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "Running in the background."})]),
    ])
    await _drain(_run(infra, jobs_client=jobs))
    assert jobs.calls == [("review_repos", {"repos": ["octocat/hello"], "limit": 2}, "c-1")]


async def test_spawn_job_dedup_and_fanout_cap():
    # Three identical spawn_job calls in ONE step -> enqueued once (dedup), not thrice.
    jobs = FakeJobs()
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("spawn_job", {"kind": "review_repos"}),
                                {"id": "call_spawn_job_b", "type": "function",
                                 "function": {"name": "spawn_job", "arguments": json.dumps({"kind": "review_repos"})}},
                                {"id": "call_spawn_job_c", "type": "function",
                                 "function": {"name": "spawn_job", "arguments": json.dumps({"kind": "review_repos"})}}]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "Started one background review."})]),
    ])
    out = (await _drain(_run(infra, jobs_client=jobs))).decode("utf-8")
    assert len(jobs.calls) == 1                  # deduped identical (kind, spec)
    assert "duplicate spawn_job" in out


async def test_spawn_job_failsoft_when_unconfigured():
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("spawn_job", {"kind": "review_repos"})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "I'll review them inline instead."})]),
    ])
    out = (await _drain(_run(infra, jobs_client=None))).decode("utf-8")
    assert "not configured" in out              # nudged to do it inline
    assert "inline" in _content(out)


async def test_spawn_job_enqueue_error_falls_back_to_inline():
    jobs = FakeJobs(status=400, body={"detail": "unknown job kind 'nope'"})
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("spawn_job", {"kind": "review_repos"})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "I'll do it inline."})]),
    ])
    out = (await _drain(_run(infra, jobs_client=jobs))).decode("utf-8")
    assert jobs.calls and "enqueue failed" in out
