"""
tests/test_subagent.py — the per-repo bounded tool loop, with a scripted fake
/complete and a fake MCP. No network, no mcp SDK.
"""
import json

from harness.subagent import review_one
from harness.prompts import REVIEW_FIELDS


class FakeInfra:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def complete(self, payload):
        self.calls.append(payload)
        return self.script.pop(0)


class FakeMCP:
    prefix = "mcp__github__"
    started = True

    def __init__(self):
        self.calls = []

    def schemas(self):
        return [{"type": "function", "function": {
            "name": "mcp__github__get_file_contents",
            "parameters": {"type": "object", "properties": {}}}}]

    def owns(self, name):
        return name.startswith(self.prefix)

    async def call(self, name, args):
        self.calls.append((name, args))
        return True, '{"content": "# README\\nA thing"}'


def _msg(content=None, tool_calls=None):
    return {"choices": [{"message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}]}


def _tc(tc_id, name, args):
    return {"id": tc_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def test_review_fields_allowlist_has_the_dossier_columns():
    for f in ("name", "summary", "role", "tech_stack", "highlights", "languages", "repo_url", "stars"):
        assert f in REVIEW_FIELDS


async def test_submits_immediately():
    infra = FakeInfra([_msg(tool_calls=[_tc("1", "submit_review",
        {"name": "repo", "summary": "does X", "tech_stack": "Python", "summary_extra": "ignored"})])])
    out = await review_one("me/repo", focus=None, infra=infra, mcp=FakeMCP(), max_steps=5)
    assert out == {"name": "repo", "summary": "does X", "tech_stack": "Python"}  # allowlisted


async def test_reads_then_submits_and_feeds_tool_result_back():
    infra = FakeInfra([
        _msg(tool_calls=[_tc("1", "mcp__github__get_file_contents",
                             {"owner": "me", "repo": "r", "path": "README.md"})]),
        _msg(tool_calls=[_tc("2", "submit_review", {"name": "r", "summary": "s"})]),
    ])
    mcp = FakeMCP()
    out = await review_one("me/r", focus=None, infra=infra, mcp=mcp, max_steps=5)
    assert out == {"name": "r", "summary": "s"}
    assert mcp.calls == [("mcp__github__get_file_contents", {"owner": "me", "repo": "r", "path": "README.md"})]
    # the 2nd /complete turn carries a tool-role result for the read
    assert any(m.get("role") == "tool" for m in infra.calls[1]["messages"])


async def test_empty_fields_are_dropped():
    infra = FakeInfra([_msg(tool_calls=[_tc("1", "submit_review",
        {"name": "r", "summary": "s", "role": "", "languages": None})])])
    out = await review_one("me/r", focus=None, infra=infra, mcp=FakeMCP(), max_steps=5)
    assert out == {"name": "r", "summary": "s"}


async def test_no_submit_returns_none():
    infra = FakeInfra([_msg(content="This repo looks great.")])  # no tool_calls
    out = await review_one("me/r", focus=None, infra=infra, mcp=FakeMCP(), max_steps=3)
    assert out is None


async def test_salvage_forces_a_submit_when_budget_exhausted():
    # The model reads every step and never submits -> after max_steps the salvage
    # turn (submit_review only) recovers a partial review instead of dropping the
    # repo silently. Regression for the live "hit max_steps without submit_review".
    read = _msg(tool_calls=[_tc("r", "mcp__github__get_file_contents",
                                {"owner": "me", "repo": "r", "path": "README.md"})])
    infra = FakeInfra([
        read, read,                                                  # 2 reads, never submits
        _msg(tool_calls=[_tc("s", "submit_review", {"name": "r", "summary": "partial"})]),  # salvage
    ])
    out = await review_one("me/r", focus=None, infra=infra, mcp=FakeMCP(), max_steps=2)
    assert out == {"name": "r", "summary": "partial"}               # salvaged, not None
    last = infra.calls[-1]                                          # the salvage /complete
    assert [t["function"]["name"] for t in last["tools"]] == ["submit_review"]   # only submit offered
    assert last["reasoning_effort"] == "medium"                     # bumped from a low reviewer effort
    assert any("submit_review NOW" in (m.get("content") or "")
               for m in last["messages"] if m.get("role") == "user")


async def test_salvage_that_still_refuses_returns_none():
    # If even the forced-submit turn won't submit, we return None (no worse than before).
    read = _msg(tool_calls=[_tc("r", "mcp__github__get_file_contents", {"path": "x"})])
    infra = FakeInfra([read, _msg(content="I really can't assess this.")])
    out = await review_one("me/r", focus=None, infra=infra, mcp=FakeMCP(), max_steps=1)
    assert out is None


async def test_stars_string_coerced_or_dropped_not_422():
    # "1,200" -> 1200 (kept); "1.2k" -> dropped, so it can't 422 the dossier write.
    infra = FakeInfra([_msg(tool_calls=[_tc("1", "submit_review",
        {"name": "r", "summary": "s", "stars": "1,200"})])])
    assert (await review_one("me/r", focus=None, infra=infra, mcp=FakeMCP(), max_steps=3))["stars"] == 1200

    infra2 = FakeInfra([_msg(tool_calls=[_tc("1", "submit_review",
        {"name": "r", "summary": "s", "stars": "1.2k"})])])
    out2 = await review_one("me/r", focus=None, infra=infra2, mcp=FakeMCP(), max_steps=3)
    assert "stars" not in out2 and out2 == {"name": "r", "summary": "s"}
