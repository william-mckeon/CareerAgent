"""
tests/test_agent_loop.py

Exercises the full agentic loop with a SCRIPTED model (fake infra.complete) and
a fake dossier — proving the control flow end to end (tool call -> permission
gate -> execute -> feed result back -> final answer -> SSE) without a real model
or network. When careeragent-infra's /complete endpoint lands, only the real
model is swapped in; this flow is already verified.
"""
from agent import loop as agent_loop


class FakeInfra:
    """Returns scripted completions in order; records how many turns ran."""

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
        return 200, {"content": "# Profile\n- Built things.", "version": 1}

    async def search_applications(self, params):
        self.calls.append(("search_applications", params))
        return 200, [{"company": "Stripe", "title": "AI Eng", "status": "applied"}]

    async def save_resume(self, aid, content):
        self.calls.append(("save_resume", aid, content))
        return 200, {"version": 1}


def _tool_call(name, arguments_json):
    return {"id": f"call_{name}", "type": "function",
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


async def test_loop_calls_a_tool_then_answers():
    infra = FakeInfra([
        _completion(tool_calls=[_tool_call("search_applications", '{"q": "fintech"}')]),
        _completion(content="You have 1 application: Stripe."),
    ])
    dossier = FakeDossier()

    out = await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "what have I applied to?"}],
        mode="acceptEdits", persona="You are CareerAgent.",
        infra_client=infra, dossier_client=dossier,
    ))

    text = out.decode("utf-8")
    assert infra.calls == 2                                   # tool turn + answer turn
    # The tool actually ran, with the model's query threaded through.
    search_calls = [c for c in dossier.calls if isinstance(c, tuple) and c[0] == "search_applications"]
    assert search_calls and search_calls[0][1].get("q") == "fintech"
    assert "search_applications" in text                      # tool activity on reasoning channel
    assert "You have 1 application: Stripe." in text          # final answer on content channel
    assert "[DONE]" in text                                   # clean terminal


async def test_loop_answers_with_no_tool_call():
    infra = FakeInfra([_completion(content="Hi! Tell me about your background.")])
    dossier = FakeDossier()

    out = await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "hello"}],
        mode="plan", persona="You are CareerAgent.",
        infra_client=infra, dossier_client=dossier,
    ))

    text = out.decode("utf-8")
    assert infra.calls == 1
    assert "Hi! Tell me about your background." in text
    assert "[DONE]" in text


async def test_plan_mode_blocks_a_write_tool_and_feeds_back_denial():
    # Model tries to save a resume while in read-only plan mode. The permission
    # engine must block it; the loop feeds the denial back and the model wraps up.
    infra = FakeInfra([
        _completion(tool_calls=[_tool_call("save_resume", '{"application_id": "x", "content": "CV"}')]),
        _completion(content="I can't edit in read-only mode — enable edits first."),
    ])
    dossier = FakeDossier()

    out = await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "save my resume"}],
        mode="plan", persona="You are CareerAgent.",
        infra_client=infra, dossier_client=dossier,
    ))

    text = out.decode("utf-8")
    # The write must NOT have executed against dossier.
    assert not any(isinstance(c, tuple) and c[0] == "save_resume" for c in dossier.calls)
    assert "save_resume" not in [c for c in dossier.calls if isinstance(c, str)]
    assert "[DONE]" in text


async def test_loop_handles_model_error_gracefully():
    class BoomInfra:
        calls = 0
        async def complete(self, payload):
            raise RuntimeError("model down")

    out = await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "hi"}],
        mode="acceptEdits", persona="p",
        infra_client=BoomInfra(), dossier_client=FakeDossier(),
    ))
    text = out.decode("utf-8")
    assert "[DONE]" in text                       # stream still terminates cleanly
    assert "couldn't reach the model" in text     # user-facing fallback


class FakeMCP:
    def __init__(self):
        self.calls = []

    def schemas(self):
        return [{"type": "function", "function": {
            "name": "mcp__github__list_repos", "description": "list the user's repos",
            "parameters": {"type": "object", "properties": {}}}}]

    def owns(self, name):
        return name.startswith("mcp__github__")

    async def call(self, name, args):
        self.calls.append((name, args))
        return True, '[{"name": "openagent"}]'


async def test_loop_routes_mcp_tool_to_mcp_client_not_dossier():
    infra = FakeInfra([
        _completion(tool_calls=[_tool_call("mcp__github__list_repos", "{}")]),
        _completion(content="You have 1 repo: openagent."),
    ])
    dossier = FakeDossier()
    mcp = FakeMCP()
    out = await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "list my repos"}],
        mode="acceptEdits", persona="p",
        infra_client=infra, dossier_client=dossier, mcp_client=mcp,
    ))
    text = out.decode("utf-8")
    assert mcp.calls == [("mcp__github__list_repos", {})]   # routed to MCP, not dossier
    assert dossier.calls == ["read_profile"]                # dossier only saw the profile load
    assert "mcp__github__list_repos" in text                # tool activity on the reasoning channel
    assert "You have 1 repo: openagent." in text
