"""
tests/test_loop_hygiene.py — Phase 2 (loop hygiene).

Retry/backoff in InfraClient.complete (httpx MockTransport, no real sleep),
tool-arg coercion/validation, parallel read dispatch, identical-repeat
loop-detection, and the finish_answer-as-JSON unwrap. See
careeragent-api/specs/0004-loop-hygiene.md.
"""
import json

import httpx
import pytest

from agent import loop as agent_loop
from agent import tools
from client.infra import InfraClient


# ============================================================ retry / backoff
def _infra_with(handler, **kw):
    c = InfraClient(url="http://x", api_key="k", backoff_base=0.0, backoff_cap=0.0, **kw)
    c._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    return c


async def test_complete_retries_429_then_succeeds():
    n = {"c": 0}

    def handler(request):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"e": "throttled"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    c = _infra_with(handler, max_retries=3)
    out = await c.complete({"messages": []})
    assert n["c"] == 2                                       # retried once, then succeeded
    assert out["choices"][0]["message"]["content"] == "ok"
    await c._http_client.aclose()


async def test_complete_does_not_retry_422():
    n = {"c": 0}

    def handler(request):
        n["c"] += 1
        return httpx.Response(422, json={"detail": "bad request"})

    c = _infra_with(handler, max_retries=3)
    with pytest.raises(httpx.HTTPStatusError):
        await c.complete({"messages": []})
    assert n["c"] == 1                                       # a 422 is a bug, not a blip — no retry
    await c._http_client.aclose()


async def test_complete_raises_after_exhausting_5xx_retries():
    n = {"c": 0}

    def handler(request):
        n["c"] += 1
        return httpx.Response(503, json={"e": "down"})

    c = _infra_with(handler, max_retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        await c.complete({"messages": []})
    assert n["c"] == 3                                       # initial + 2 retries, then raise
    await c._http_client.aclose()


# ============================================================ arg coercion
def test_coerce_stringified_json_array_arg():
    args, err = tools.coerce_and_check("review_repos", {"repos": '["me/a","me/b"]'})
    assert err is None and args["repos"] == ["me/a", "me/b"]


def test_coerce_missing_required_returns_error():
    args, err = tools.coerce_and_check("get_application", {})   # requires application_id
    assert err and "application_id" in err


def test_coerce_unparseable_left_as_is_no_crash():
    args, err = tools.coerce_and_check("review_repos", {"repos": "not json"})
    assert err is None and args["repos"] == "not json"         # left for dispatch to handle


def test_coerce_unknown_or_mcp_tool_passes_through():
    args, err = tools.coerce_and_check("mcp__github__list_repos", {"owner": "me"})
    assert err is None and args == {"owner": "me"}


# ============================================================ loop-level harness
class RecordingInfra:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def complete(self, payload):
        r = self._responses[self.calls]
        self.calls += 1
        return r


class FakeDossier:
    def __init__(self):
        self.calls = []

    async def read_profile(self):
        self.calls.append("read_profile")
        return 200, {"content": "# Profile", "version": 1}

    async def search_applications(self, params):
        self.calls.append("search_applications")
        return 200, []

    async def search_projects(self, params):
        self.calls.append(("search_projects", params))
        return 200, [{"id": "p1", "name": "OpenAgent"}]

    async def edit_profile(self, old, new, replace_all=False):
        self.calls.append("edit_profile")
        return 200, {"content": "# Profile edited", "version": 2}   # receipt -> verified write


def _tc(name, args):
    return {"id": f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _completion(content="", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


async def _drain(gen):
    out = b""
    async for chunk in gen:
        out += chunk
    return out


def _run(infra, dossier, mode="acceptEdits"):
    return agent_loop.run_agent(
        messages=[{"role": "user", "content": "go"}], mode=mode, persona="CareerAgent.",
        infra_client=infra, dossier_client=dossier, max_steps=40)


async def test_all_reads_batch_is_dispatched():
    # A batch of 2 independent reads runs (parallel path); both hit the dossier.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("read_profile", {}), _tc("search_projects", {"q": "ai"})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "done"})]),
    ])
    dossier = FakeDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    # read_profile runs twice: once at loop startup, once IN the batch (proves the
    # batch's read actually dispatched); search_projects only comes from the batch.
    assert dossier.calls.count("read_profile") == 2
    assert sum(1 for c in dossier.calls if isinstance(c, tuple) and c[0] == "search_projects") == 1
    assert "done" in out


async def test_identical_repeat_call_is_skipped():
    # The same call on the next step must NOT re-execute — it's corrected instead.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("search_applications", {"q": "x"})]),
        _completion(tool_calls=[_tc("search_applications", {"q": "x"})]),   # identical -> skipped
        _completion(tool_calls=[_tc("finish_answer", {"summary": "done"})]),
    ])
    dossier = FakeDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    assert dossier.calls.count("search_applications") == 1     # duplicate not executed
    assert "duplicate call" in out
    assert "done" in out


async def test_persistent_repeat_breaks_to_synthesis():
    # Two identical repeats in a row -> spin_abort -> synthesis (not a dead loop).
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("search_applications", {"q": "x"})]),
        _completion(tool_calls=[_tc("search_applications", {"q": "x"})]),   # dup 1 (skipped)
        _completion(tool_calls=[_tc("search_applications", {"q": "x"})]),   # dup 2 -> abort
        _completion(content="Here's what I found so far."),                 # synthesis turn
    ])
    dossier = FakeDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    assert dossier.calls.count("search_applications") == 1
    assert "Here's what I found so far." in out


async def test_finish_answer_json_blob_is_unwrapped():
    # gpt-oss sometimes emits finish_answer as text; we stream the summary, not raw JSON.
    infra = RecordingInfra([_completion(content='{"summary": "Here is your report."}')])
    out = (await _drain(_run(infra, FakeDossier(), mode="plan"))).decode("utf-8")
    assert "Here is your report." in out
    assert '{"summary"' not in out


async def test_normal_json_answer_is_not_mangled():
    # A normal prose answer that merely mentions JSON is streamed unchanged.
    infra = RecordingInfra([_completion(content="Your profile has 3 projects.")])
    out = (await _drain(_run(infra, FakeDossier(), mode="plan"))).decode("utf-8")
    assert "Your profile has 3 projects." in out


# ============================================================ review fixes
def test_empty_string_required_arg_is_valid_not_missing():
    # edit_resume new_string="" means DELETE the matched text — must NOT be flagged
    # as missing; a truly-absent required arg still errors.
    _, ok_err = tools.coerce_and_check(
        "edit_resume", {"application_id": "a1", "old_string": "- bullet\n", "new_string": ""})
    assert ok_err is None
    _, miss_err = tools.coerce_and_check("edit_resume", {"application_id": "a1", "old_string": "x"})
    assert miss_err and "new_string" in miss_err


async def test_duplicate_read_batch_is_skipped():
    # An identical READ batch on repeat is caught (not re-run to the step budget).
    batch = [_tc("read_profile", {}), _tc("search_projects", {"q": "ai"})]
    infra = RecordingInfra([
        _completion(tool_calls=batch),
        _completion(tool_calls=batch),                                     # identical -> skipped
        _completion(tool_calls=[_tc("finish_answer", {"summary": "done"})]),
    ])
    dossier = FakeDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    assert sum(1 for c in dossier.calls if isinstance(c, tuple) and c[0] == "search_projects") == 1
    assert "duplicate read batch" in out and "done" in out


class ErroringDossier(FakeDossier):
    def __init__(self):
        super().__init__()
        self._fail_next = True

    async def search_applications(self, params):
        self.calls.append("search_applications")
        if self._fail_next:
            self._fail_next = False
            return 500, {"detail": "transient"}
        return 200, []


async def test_retry_after_tool_error_is_not_suppressed():
    # The first call errors (500 -> ok=False); the model retries the SAME call,
    # which must RUN again rather than be suppressed as a duplicate.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("search_applications", {"q": "x"})]),   # errors
        _completion(tool_calls=[_tc("search_applications", {"q": "x"})]),   # retry -> must run
        _completion(tool_calls=[_tc("finish_answer", {"summary": "ok"})]),
    ])
    dossier = ErroringDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    assert dossier.calls.count("search_applications") == 2                 # retried, not suppressed
    assert "ok" in out


async def test_unwrap_leaves_multikey_json_untouched():
    # A JSON object with keys beyond {summary, open_items} is a real answer —
    # streamed unchanged, not collapsed to just the summary.
    infra = RecordingInfra([_completion(content='{"summary": "s", "data": [1, 2, 3]}')])
    out = (await _drain(_run(infra, FakeDossier(), mode="plan"))).decode("utf-8")
    # not collapsed to just the summary — the sibling data survived (would be absent
    # if _unwrap_finish_json had wrongly unwrapped it).
    assert "[1, 2, 3]" in out


# ==================================== live-smoke fixes (convergence / read hygiene)
async def test_readonly_spin_triggers_converge_nudge():
    # Many DIFFERENT reads in a row (each a distinct signature, so identical-repeat
    # detection can't see them) with no write/finish -> after READ_STREAK_CAP steps
    # the model gets a [converge] nudge instead of burning the whole step budget.
    reads = [_completion(tool_calls=[_tc("search_projects", {"q": f"term{i}"})])
             for i in range(agent_loop.READ_STREAK_CAP)]
    infra = RecordingInfra(reads + [_completion(tool_calls=[_tc("finish_answer", {"summary": "done"})])])
    dossier = FakeDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    assert "converge nudge" in out                                  # the guard fired
    # every distinct read still executed (nudged, not suppressed)
    assert sum(1 for c in dossier.calls if isinstance(c, tuple) and c[0] == "search_projects") \
        == agent_loop.READ_STREAK_CAP
    assert "done" in out


async def test_redundant_read_profile_is_short_circuited():
    # The profile is pinned in the system prompt; a read_profile with no prior edit
    # this turn is served a reminder, NOT a second dossier round-trip.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("read_profile", {})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "done"})]),
    ])
    dossier = FakeDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    assert dossier.calls.count("read_profile") == 1                 # only the startup read
    assert "already in context" in out
    assert "done" in out


async def test_read_profile_allowed_after_an_edit():
    # After the model edits the profile this turn, a real read_profile IS allowed
    # (it needs the post-edit version) — the short-circuit only guards redundant reads.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("edit_profile", {"old_string": "a", "new_string": "b"})]),
        _completion(tool_calls=[_tc("read_profile", {})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "done"})]),
    ])
    dossier = FakeDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    assert dossier.calls.count("read_profile") == 2                 # startup + the post-edit read
    assert "already in context" not in out


async def test_empty_finish_is_challenged_then_completes():
    # finish_answer with no summary and no content is a silent punt -> challenged;
    # the model then gives a real summary.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("finish_answer", {"summary": ""})]),      # empty -> challenged
        _completion(tool_calls=[_tc("finish_answer", {"summary": "Here's the plan."})]),
    ])
    out = (await _drain(_run(infra, FakeDossier()))).decode("utf-8")
    assert infra.calls == 2                                          # not accepted blank at step 0
    assert "empty finish challenged" in out
    assert "Here's the plan." in out


async def test_empty_finish_cap_lets_a_stubborn_blank_through():
    # A model that keeps finishing blank still terminates after the cap (no dead loop).
    blank = _completion(tool_calls=[_tc("finish_answer", {"summary": ""})])
    infra = RecordingInfra([blank] * (agent_loop.COMPLETION_CHALLENGE_CAP + 1))
    out = (await _drain(_run(infra, FakeDossier()))).decode("utf-8")
    assert infra.calls == agent_loop.COMPLETION_CHALLENGE_CAP + 1    # CAP challenges, then accept
