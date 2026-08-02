"""
tests/test_plan_vs_act.py — Phase 7 #20: plan-vs-act (propose → confirm → execute).

In read-only plan mode the coach investigates, then `propose_plan`s a structured
approach and PAUSES (a plan_proposal suspend frame). On approval the SAME run
resumes in acceptEdits with the proposal's steps seeded as the checklist and
executes them; on decline it stays read-only. propose_plan is control-intercepted
(works in plan mode), must be solo, and needs steps. See specs/0011-plan-vs-act.md.
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


class WriteDossier(FakeDossier):
    """Records writes so a test can assert an approved plan actually executed."""
    def __init__(self):
        self.writes = []

    async def save_profile(self, content):
        self.writes.append(("save_profile", content))
        return 200, {"content": content, "version": 2}    # a receipt -> verified write


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


def _suspend_event(out: str):
    for line in out.splitlines():
        if not line.startswith("data:"):
            continue
        p = line[5:].strip()
        if not p.startswith("{"):
            continue
        try:
            ca = json.loads(p).get("careeragent")
        except Exception:
            continue
        if isinstance(ca, dict) and ca.get("event") == "suspend":
            return ca
    return None


def _run(infra, dossier, *, mode="plan", approval=None, outcome=None):
    return agent_loop.run_agent(
        messages=[{"role": "user", "content": "update my profile with my new role"}], mode=mode,
        persona="CareerAgent.", infra_client=infra, dossier_client=dossier, max_steps=40,
        guardian_enabled=False, approval=approval, outcome=outcome)


# ---------------------------------------------------------------- the tool
def test_propose_plan_is_a_non_mutating_control_tool_in_every_mode():
    assert "propose_plan" in tools.CONTROL_TOOLS
    assert not permissions.is_mutating("propose_plan")
    for mode in ("plan", "default", "acceptEdits", "bypass"):
        names = {s["function"]["name"] for s in tools.schemas_for_mode(mode)}
        assert "propose_plan" in names


# ---------------------------------------------------------------- pause
async def test_propose_plan_suspends_with_plan_payload():
    infra = RecordingInfra([_completion(tool_calls=[_tc("propose_plan", {
        "summary": "Tailor the resume to the JD",
        "steps": [{"content": "Draft metric-first bullets"}, {"content": "Save the tailored resume"}]})])])
    outcome = {}
    out = (await _drain(_run(infra, FakeDossier(), mode="plan", outcome=outcome))).decode("utf-8")
    sus = _suspend_event(out)
    assert sus is not None
    assert sus["pending_kind"] == "plan_proposal"
    assert sus["pending_call_id"] == "call_propose_plan"
    assert sus["payload"]["summary"] == "Tailor the resume to the JD"
    steps = sus["payload"]["steps"]
    assert [s["content"] for s in steps] == ["Draft metric-first bullets", "Save the tailored resume"]
    assert all(s["status"] == "pending" for s in steps)          # seeded as an open checklist
    assert outcome.get("value") == agent_loop.OUTCOME_PAUSED
    assert infra.calls == 1                                       # paused after one model call


async def test_propose_plan_accepts_stringified_steps():
    # The weak model may emit `steps` as a JSON STRING (propose_plan is intercepted
    # before the dossier-tool arg coercion) — _coerce_steps must still handle it.
    infra = RecordingInfra([_completion(tool_calls=[
        _tc("propose_plan", {"summary": "x", "steps": '[{"content": "do the thing"}]'})])])
    out = (await _drain(_run(infra, FakeDossier(), mode="plan"))).decode("utf-8")
    sus = _suspend_event(out)
    assert sus is not None and sus["payload"]["steps"][0]["content"] == "do the thing"


# ---------------------------------------------------------------- approve → execute
async def test_approved_plan_resumes_in_edit_mode_and_executes():
    # The paused snapshot ends with the UNANSWERED propose_plan call; the resume
    # carries approval granted + the elevated acceptEdits mode (set by sessions).
    resumed = [
        {"role": "user", "content": "update my profile with my new role"},
        {"role": "assistant", "content": "",
         "tool_calls": [_tc("propose_plan", {"summary": "Add the role",
                                             "steps": [{"content": "Add Senior Engineer to the profile"}]})]},
    ]
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("save_profile", {"content": "# Profile\n- Senior Engineer"})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "Added the role to your profile."})]),
    ])
    dossier = WriteDossier()
    out = (await _drain(agent_loop.run_agent(
        messages=resumed, mode="acceptEdits", persona="X", infra_client=infra,
        dossier_client=dossier, max_steps=40, guardian_enabled=False,
        approval={"call_id": "call_propose_plan", "granted": True}))).decode("utf-8")
    assert any(w[0] == "save_profile" for w in dossier.writes)    # executed the plan in edit mode
    assert "Added the role to your profile." in _content(out)
    assert '"plan_update"' in out                                 # the approved steps were seeded
    assert _suspend_event(out) is None


async def test_declined_plan_stays_read_only():
    resumed = [
        {"role": "user", "content": "update my profile"},
        {"role": "assistant", "content": "",
         "tool_calls": [_tc("propose_plan", {"summary": "Add the role",
                                             "steps": [{"content": "Add Senior Engineer"}]})]},
    ]
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("finish_answer",
                                    {"summary": "Okay, I'll hold off. What would you like to adjust?"})]),
    ])
    dossier = WriteDossier()
    out = (await _drain(agent_loop.run_agent(
        messages=resumed, mode="plan", persona="X", infra_client=infra,
        dossier_client=dossier, max_steps=40, guardian_enabled=False,
        approval={"call_id": "call_propose_plan", "granted": False}))).decode("utf-8")
    assert dossier.writes == []                                   # nothing changed on decline
    assert "hold off" in _content(out)


async def test_write_is_denied_in_plan_mode_before_approval():
    # A direct write in plan mode is hard-denied — the point of proposing first.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("save_profile", {"content": "x"})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "I can't change anything in plan mode."})]),
    ])
    dossier = WriteDossier()
    out = (await _drain(_run(infra, dossier, mode="plan"))).decode("utf-8")
    assert dossier.writes == []                                   # denied before dispatch
    assert "plan mode" in _content(out)                          # the coach says it can't change things


# ---------------------------------------------------------------- guards
async def test_propose_plan_without_steps_is_nudged():
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("propose_plan", {"summary": "x", "steps": []})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "done"})]),
    ])
    out = (await _drain(_run(infra, FakeDossier(), mode="plan"))).decode("utf-8")
    assert _suspend_event(out) is None
    assert "without steps" in out                                # the reasoning nudge line
    assert "done" in _content(out)


async def test_batched_propose_plan_is_nudged_not_suspended():
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("propose_plan", {"summary": "x", "steps": [{"content": "a"}]}),
                                _tc("read_profile", {})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "done"})]),
    ])
    out = (await _drain(_run(infra, FakeDossier(), mode="plan"))).decode("utf-8")
    assert _suspend_event(out) is None
    assert "called alone" in out                                 # the reasoning nudge line
    assert "done" in _content(out)
