"""
tests/test_typed_streaming.py — P7 #19 typed structured streaming.

The loop emits typed `careeragent` progress frames (plan_update / tool_start /
tool_result) ALONGSIDE the existing plain-text reasoning — purely additive, so an
un-upgraded frontend is unaffected. Verifies the frames appear, carry their
payload, and don't disturb the answer/finish path.
"""
import json

from agent import loop as agent_loop


class FakeInfra:
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

    async def list_preferences(self):
        return 200, []

    async def search_applications(self, params):
        return 200, [{"company": "Acme"}]


def _tool_call(name, args):
    return {"id": f"call_{name}", "type": "function", "function": {"name": name, "arguments": args}}


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


async def test_loop_emits_typed_frames_alongside_reasoning():
    infra = FakeInfra([
        _completion(tool_calls=[_tool_call(
            "update_plan", '{"steps": [{"content": "Draft the resume", "status": "in_progress"}]}')]),
        _completion(tool_calls=[_tool_call("search_applications", '{"q": "ai"}')]),
        _completion(tool_calls=[_tool_call("finish_answer", '{"summary": "All done."}')]),
    ])
    out = (await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "go"}],
        mode="acceptEdits", persona="p", infra_client=infra, dossier_client=FakeDossier(),
    ))).decode("utf-8")

    # Typed frames are present (namespaced careeragent events).
    assert '"event": "plan_update"' in out
    assert '"event": "tool_start"' in out
    assert '"event": "tool_result"' in out
    # plan_update carries the FULL plan (not the "N steps" summary string).
    assert '"content": "Draft the resume"' in out and '"status": "in_progress"' in out
    # The answer still streams on delta.content; the plain reasoning is still emitted.
    assert "All done." in out
    assert "search_applications" in out          # the plain-text reasoning line, unchanged


async def test_typed_frames_are_namespaced_not_content():
    # A typed frame must never carry OpenAI `choices`/`delta.content` — else it would
    # be captured into the transcript. It rides only the `careeragent` namespace.
    infra = FakeInfra([
        _completion(tool_calls=[_tool_call(
            "update_plan", '{"steps": [{"content": "step one", "status": "pending"}]}')]),
        _completion(tool_calls=[_tool_call("finish_answer", '{"summary": "ok"}')]),
    ])
    out = (await _drain(agent_loop.run_agent(
        messages=[{"role": "user", "content": "go"}],
        mode="acceptEdits", persona="p", infra_client=infra, dossier_client=FakeDossier(),
    ))).decode("utf-8")
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("data: ") or line.endswith("[DONE]"):
            continue
        try:
            obj = json.loads(line[len("data: "):])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("careeragent", {}).get("event") in (
                "plan_update", "tool_start", "tool_result", "step"):
            assert "choices" not in obj      # typed frame carries no content channel
