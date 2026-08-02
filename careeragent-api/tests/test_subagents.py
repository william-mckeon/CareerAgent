"""
tests/test_subagents.py — P6 #8 general subagent delegation.

Covers run_subagent (returns only final text; role-restricted, read-only; salvages
on budget) AND the spawn_subagent intercept in the coach loop (fan-out cap, depth/
disable hides the tool, a read-only child never satisfies the verified-completion
gate — i.e. can't launder a write claim into the parent ledger).
"""
from agent import loop as agent_loop
from agent import roster, subagents, tools


# --------------------------------------------------------------------- fakes
class FakeInfra:
    """Scripted completions in order; records each payload it was sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.payloads = []

    async def complete(self, payload):
        self.payloads.append(payload)
        r = self._responses[self.calls]
        self.calls += 1
        return r


class FakeDossier:
    def __init__(self):
        self.calls = []

    async def read_profile(self):
        self.calls.append("read_profile")
        return 200, {"content": "# Profile\n- Built a Python API.", "version": 1}

    async def search_projects(self, params):
        self.calls.append(("search_projects", params))
        return 200, [{"name": "OpenAgent", "summary": "an agent OS"}]

    async def get_project(self, pid):
        self.calls.append(("get_project", pid))
        return 200, {"name": "OpenAgent", "summary": "an agent OS"}

    async def search_applications(self, params):
        self.calls.append(("search_applications", params))
        return 200, [{"company": "Stripe"}]

    async def save_resume(self, aid, content):
        self.calls.append(("save_resume", aid, content))
        return 200, {"version": 2}


def _tool_call(name, arguments_json, cid=None):
    return {"id": cid or f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": arguments_json}}


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


# --------------------------------------------------------- run_subagent (unit)
async def test_run_subagent_returns_finish_text():
    infra = FakeInfra([_completion(tool_calls=[
        _tool_call("finish_answer", '{"summary": "Bullet 1 sharpened; bullet 2 needs a real metric."}')])])
    out = await subagents.run_subagent(
        task="Critique these bullets: ...", role="bullet-critic",
        infra_client=infra, dossier_client=FakeDossier())
    assert out == "Bullet 1 sharpened; bullet 2 needs a real metric."


async def test_run_subagent_plain_reply_is_the_answer():
    infra = FakeInfra([_completion(content="The JD wants Go; the profile doesn't show it — a real gap.")])
    out = await subagents.run_subagent(
        task="Analyze this JD", role="jd-gap-analyzer",
        infra_client=infra, dossier_client=FakeDossier())
    assert "real gap" in out


async def test_run_subagent_rejects_a_tool_outside_its_role():
    # bullet-critic's toolset is {search_projects, get_project}; a search_applications
    # call must be refused (teaching msg) and NEVER dispatched to dossier.
    dossier = FakeDossier()
    infra = FakeInfra([
        _completion(tool_calls=[_tool_call("search_applications", "{}")]),
        _completion(tool_calls=[_tool_call("finish_answer", '{"summary": "done"}')]),
    ])
    out = await subagents.run_subagent(
        task="critique", role="bullet-critic", infra_client=infra, dossier_client=dossier)
    assert out == "done"
    assert not any(isinstance(c, tuple) and c[0] == "search_applications" for c in dossier.calls)


async def test_run_subagent_dispatches_an_allowed_read():
    dossier = FakeDossier()
    infra = FakeInfra([
        _completion(tool_calls=[_tool_call("search_projects", '{"q": "python"}')]),
        _completion(tool_calls=[_tool_call("finish_answer", '{"summary": "found OpenAgent"}')]),
    ])
    out = await subagents.run_subagent(
        task="critique", role="bullet-critic", infra_client=infra, dossier_client=dossier)
    assert out == "found OpenAgent"
    assert any(isinstance(c, tuple) and c[0] == "search_projects" for c in dossier.calls)


async def test_run_subagent_salvages_when_out_of_steps():
    # Never calls finish_answer — keeps reading — so the salvage (tools-disabled) fires.
    read = _completion(tool_calls=[_tool_call("search_projects", "{}")])
    infra = FakeInfra([read, read, _completion(content="Best-effort critique from what I saw.")])
    out = await subagents.run_subagent(
        task="critique", role="bullet-critic", infra_client=infra,
        dossier_client=FakeDossier(), max_steps=2)
    assert "Best-effort critique" in out
    # Last call was the tools-disabled salvage.
    assert infra.payloads[-1]["tools"] == []


async def test_unknown_role_returns_a_note():
    out = await subagents.run_subagent(
        task="x", role="nonexistent", infra_client=FakeInfra([]), dossier_client=FakeDossier())
    assert "unknown subagent role" in out.lower()


async def test_empty_finish_salvages_instead_of_returning_blank():
    # P6-review fix: a child that finishes with an EMPTY summary must not return "" —
    # it salvages the work so the parent never mistakes silence for a clean result.
    infra = FakeInfra([
        _completion(tool_calls=[_tool_call("finish_answer", '{"summary": ""}')]),
        _completion(content="Salvaged critique from what I gathered."),   # tools-disabled salvage
    ])
    out = await subagents.run_subagent(
        task="critique", role="bullet-critic", infra_client=infra, dossier_client=FakeDossier())
    assert out == "Salvaged critique from what I gathered."
    assert infra.payloads[-1]["tools"] == []          # the salvage call was tools-disabled


def test_schemas_for_role_are_read_only():
    for role in roster.ROLE_NAMES:
        names = {s["function"]["name"] for s in roster.schemas_for_role(role)}
        assert "finish_answer" in names
        assert not (names & tools.WRITE_TOOLS)          # no write tool ever
        assert "ask_user" not in names                  # a child can't pause the parent
        assert "spawn_subagent" not in names            # a child can't spawn (depth cap)
        assert "read_profile" not in names              # profile is injected, not read


# --------------------------------------------------- spawn intercept (in-loop)
async def test_coach_delegates_then_continues():
    # coach spawns reviewer -> child finishes -> coach finishes. One shared FakeInfra
    # serves both (run_subagent reuses the same infra_client).
    infra = FakeInfra([
        _completion(tool_calls=[_tool_call("spawn_subagent",
                    '{"role": "reviewer", "task": "review this draft: ..."}')]),
        _completion(tool_calls=[_tool_call("finish_answer",
                    '{"summary": "Solid draft; tighten the summary line."}')]),   # child
        _completion(content="Thanks — I tightened it based on the review."),        # coach
    ])
    out = (await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "review my resume draft"}],
        mode="acceptEdits", persona="p", infra_client=infra, dossier_client=FakeDossier(),
    ))).decode("utf-8")
    assert infra.calls == 3
    assert "delegating to reviewer" in out
    assert "Thanks — I tightened it" in out
    assert "[DONE]" in out


async def test_fanout_cap_blocks_extra_delegations():
    infra = FakeInfra([
        _completion(tool_calls=[_tool_call("spawn_subagent",
                    '{"role": "reviewer", "task": "t1"}', cid="c1")]),
        _completion(tool_calls=[_tool_call("finish_answer", '{"summary": "child1"}')]),   # child 1
        _completion(tool_calls=[_tool_call("spawn_subagent",
                    '{"role": "reviewer", "task": "t2"}', cid="c2")]),   # 2nd spawn -> capped
        _completion(content="Done."),                                    # coach finishes
    ])
    out = (await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "go"}],
        mode="acceptEdits", persona="p", infra_client=infra, dossier_client=FakeDossier(),
        subagent_max_fanout=1,
    ))).decode("utf-8")
    assert "fan-out cap reached" in out
    # Only ONE child turn ran (the 2nd spawn was capped, not executed).
    assert infra.calls == 4


async def test_disabled_hides_the_tool_from_the_catalog():
    infra = FakeInfra([_completion(content="hi")])
    await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "hi"}],
        mode="acceptEdits", persona="p", infra_client=infra, dossier_client=FakeDossier(),
        subagent_enabled=False,
    ))
    names = {s["function"]["name"] for s in infra.payloads[0]["tools"]}
    assert "spawn_subagent" not in names


async def test_enabled_exposes_the_tool():
    infra = FakeInfra([_completion(content="hi")])
    await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "hi"}],
        mode="acceptEdits", persona="p", infra_client=infra, dossier_client=FakeDossier(),
        subagent_enabled=True,
    ))
    names = {s["function"]["name"] for s in infra.payloads[0]["tools"]}
    assert "spawn_subagent" in names


async def test_delegation_does_not_launder_a_write_claim():
    # A read-only child returns text; it mints NO ledger receipt. So a finish that
    # CLAIMS a save with no real write is still challenged by the completion gate.
    infra = FakeInfra([
        _completion(tool_calls=[_tool_call("spawn_subagent",
                    '{"role": "reviewer", "task": "review"}')]),
        _completion(tool_calls=[_tool_call("finish_answer", '{"summary": "child says ok"}')]),  # child
        _completion(tool_calls=[_tool_call("finish_answer",
                    '{"summary": "I have saved your updated resume."}')]),   # coach over-claims
        _completion(content="Actually I haven't changed anything yet."),      # after the challenge
    ])
    out = (await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "review and save"}],
        mode="acceptEdits", persona="p", infra_client=infra, dossier_client=FakeDossier(),
    ))).decode("utf-8")
    assert "completion challenged" in out          # the spawn did NOT satisfy the gate
    assert "Actually I haven't changed anything" in out
