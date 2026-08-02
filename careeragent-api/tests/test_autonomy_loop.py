"""
tests/test_autonomy_loop.py — Phase 1 (autonomy core).

Exercises the persist-until-done loop: finish_answer as the terminal signal,
update_plan storage + pinning, the "keep going" nudge + cap, and
synthesis-on-exhaustion (never the old punt). Scripted fake infra + dossier —
no model, no network. See careeragent-api/specs/0003-autonomy-core.md.
"""
import json

from agent import loop as agent_loop


class RecordingInfra:
    """Returns scripted completions in order and records every payload it saw."""

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
        return 200, {"content": "# Profile", "version": 1}

    async def save_resume(self, aid, content):
        self.calls.append(("save_resume", aid, content))
        return 200, {"version": 2}

    async def search_applications(self, params):
        self.calls.append("search_applications")
        return 200, []


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


def _run(infra, dossier, mode="acceptEdits", max_steps=40):
    return agent_loop.run_agent(
        messages=[{"role": "user", "content": "tailor my resume"}],
        mode=mode, persona="You are CareerAgent.",
        infra_client=infra, dossier_client=dossier, max_steps=max_steps,
    )


async def test_finish_answer_ends_the_turn():
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("finish_answer", {"summary": "All set — resume tailored."})]),
    ])
    out = (await _drain(_run(infra, FakeDossier()))).decode("utf-8")
    assert infra.calls == 1
    assert "All set — resume tailored." in out
    assert "[DONE]" in out


async def test_finish_answer_appends_open_items():
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("finish_answer",
            {"summary": "Draft done.", "open_items": ["confirm the target salary"]})]),
    ])
    out = (await _drain(_run(infra, FakeDossier()))).decode("utf-8")
    assert "Draft done." in out
    assert "Still needs you" in out and "confirm the target salary" in out


async def test_plain_reply_does_not_end_turn_while_plan_open():
    # update_plan (2 open) -> plain mid-task text (must be NUDGED, not accepted)
    # -> does the work -> finish_answer.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("update_plan", {"steps": [
            {"content": "draft resume", "status": "in_progress"},
            {"content": "save it", "status": "pending"}]})]),
        _completion(content="I think I'll start with the summary."),
        _completion(tool_calls=[_tc("save_resume", {"application_id": "a1", "content": "CV"})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "Done — saved."})]),
    ])
    dossier = FakeDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    assert infra.calls == 4                                      # nudged, not ended at the plain reply
    assert "I think I'll start with the summary." not in out     # that mid-task text was NOT the answer
    assert "Done — saved." in out
    assert any(isinstance(c, tuple) and c[0] == "save_resume" for c in dossier.calls)


async def test_plan_is_pinned_into_the_system_prompt():
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("update_plan", {"steps": [
            {"content": "rewrite bullet 3", "status": "in_progress"}]})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "done"})]),
    ])
    await _drain(_run(infra, FakeDossier()))
    system_msg = infra.payloads[1]["messages"][0]["content"]     # the 2nd turn's system message
    assert "## Current plan" in system_msg
    assert "rewrite bullet 3" in system_msg


async def test_cancelled_step_is_not_open():
    # A cancelled step must not keep the loop going.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("update_plan", {"steps": [
            {"content": "abandoned idea", "status": "cancelled"}]})]),
        _completion(content="Here's my take."),                 # plain reply, no OPEN items -> accepted
    ])
    out = (await _drain(_run(infra, FakeDossier()))).decode("utf-8")
    assert infra.calls == 2
    assert "Here's my take." in out


async def test_reminder_cap_accepts_reply_not_infinite_loop():
    responses = [_completion(tool_calls=[_tc("update_plan", {"steps": [
        {"content": "x", "status": "pending"}]})])]
    responses += [_completion(content="still thinking...") for _ in range(10)]
    infra = RecordingInfra(responses)
    out = (await _drain(_run(infra, FakeDossier()))).decode("utf-8")
    assert infra.calls == 1 + agent_loop.REMINDER_CAP + 1        # update_plan + CAP nudges + 1 accepted
    assert "still thinking..." in out
    assert "[DONE]" in out


async def test_synthesis_on_step_budget_exhaustion():
    # Model calls a tool every step and never finishes. A small budget must
    # trigger ONE synthesis turn (tools disabled), not the old punt.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("search_applications", {})]),
        _completion(tool_calls=[_tc("search_applications", {})]),
        _completion(content="Here's what I found and what's left."),   # synthesis answer
    ])
    out = (await _drain(_run(infra, FakeDossier(), max_steps=2))).decode("utf-8")
    assert infra.calls == 3                                      # 2 loop steps + 1 synthesis
    synth = infra.payloads[-1]
    assert synth["tools"] == []                                 # tools disabled on the synthesis turn
    # ...and the tool activity is FLATTENED to text so Bedrock Converse accepts a
    # tools-disabled request (no toolUse/toolResult blocks left in the messages).
    assert all(m.get("role") != "tool" for m in synth["messages"])
    assert all("tool_calls" not in m for m in synth["messages"])
    assert "could you tell me a bit more" not in out            # the old punt is gone
    assert "Here's what I found and what's left." in out
    assert "[DONE]" in out


class StrictInfra(RecordingInfra):
    """Reproduces the Bedrock Converse constraint the flatten step exists for:
    a request with tool blocks (role:tool or tool_calls) but no toolConfig
    (empty `tools`) is rejected."""

    async def complete(self, payload):
        if not payload.get("tools"):
            for m in payload["messages"]:
                if m.get("role") == "tool" or m.get("tool_calls"):
                    raise RuntimeError("Bedrock ValidationException: tool blocks without toolConfig")
        return await super().complete(payload)


async def test_synthesis_is_bedrock_valid_not_a_degraded_punt():
    # Regression for the review's high finding: the synthesis turn must not ship
    # tool blocks with tools:[] (which Bedrock rejects -> the loop would degrade to
    # the fallback punt). StrictInfra raises if it does; a clean run proves the fix.
    infra = StrictInfra([
        _completion(tool_calls=[_tc("search_applications", {})]),
        _completion(tool_calls=[_tc("search_applications", {})]),
        _completion(content="Reviewed 2 apps; one still open."),
    ])
    out = (await _drain(_run(infra, FakeDossier(), max_steps=2))).decode("utf-8")
    assert infra.calls == 3                                      # the synthesis call did NOT raise
    assert "Reviewed 2 apps; one still open." in out            # real synthesis, not the fallback line
    assert "tell me which part to take further" not in out


async def test_malformed_update_plan_keeps_previous_plan():
    # A garbage update_plan must NOT wipe a live plan (the persistence guarantee).
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("update_plan", {"steps": [
            {"content": "draft the resume", "status": "in_progress"}]})]),
        _completion(tool_calls=[_tc("update_plan", {"steps": "not-a-list"})]),   # garbage
        _completion(content="I'll just chat instead."),                          # plain -> must be NUDGED
        _completion(tool_calls=[_tc("finish_answer", {"summary": "done"})]),
    ])
    out = (await _drain(_run(infra, FakeDossier()))).decode("utf-8")
    assert "Plan unchanged" in out                       # the garbage call was rejected
    assert "I'll just chat instead." not in out          # still nudged => the plan survived
    assert infra.calls == 4 and "done" in out


async def test_replan_only_still_trips_the_cap():
    # A model that only re-plans (never runs a real tool) must still hit the nudge
    # cap and terminate — update_plan must NOT reset the counter.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("update_plan", {"steps": [{"content": "x", "status": "pending"}]})]),
        _completion(content="hmm"),                                              # nudge 1
        _completion(tool_calls=[_tc("update_plan", {"steps": [{"content": "x", "status": "pending"}]})]),
        _completion(content="hmm"),                                              # nudge 2
        _completion(content="here's my final take"),                             # cap reached -> accepted
    ])
    out = (await _drain(_run(infra, FakeDossier(), max_steps=40))).decode("utf-8")
    assert infra.calls == 5                               # tripped the cap; no runaway to max_steps
    assert "here's my final take" in out


async def test_blank_capped_reply_synthesizes_not_streams_empty():
    # If the model returns a BLANK reply at the cap, don't stream "" — synthesize.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("update_plan", {"steps": [{"content": "x", "status": "pending"}]})]),
        _completion(content="   "),   # blank, open plan -> nudge 1
        _completion(content=""),      # blank, open plan -> nudge 2
        _completion(content=""),      # blank at cap -> must NOT be accepted as answer
        _completion(content="Here's what I managed."),   # synthesis
    ])
    out = (await _drain(_run(infra, FakeDossier(), max_steps=40))).decode("utf-8")
    assert infra.payloads[-1]["tools"] == []             # last call was the synthesis turn
    assert "Here's what I managed." in out


async def test_control_tools_work_in_plan_mode():
    # finish_answer / update_plan are non-mutating -> usable even in read-only plan mode.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("finish_answer", {"summary": "analysis done"})]),
    ])
    out = (await _drain(_run(infra, FakeDossier(), mode="plan"))).decode("utf-8")
    assert "analysis done" in out
    assert "[DONE]" in out


async def test_simple_answer_still_returns_in_one_turn():
    # No plan, plain reply -> that reply is the answer (today's behavior preserved).
    infra = RecordingInfra([_completion(content="Your profile lists 3 projects.")])
    out = (await _drain(_run(infra, FakeDossier(), mode="plan"))).decode("utf-8")
    assert infra.calls == 1
    assert "Your profile lists 3 projects." in out
