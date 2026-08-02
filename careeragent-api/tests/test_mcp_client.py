"""
tests/test_mcp_client.py

Unit tests for the MCP client's pure logic — content flattening, tool
namespacing, and call routing — with a fake ClientSession injected directly, so
no `mcp` SDK, no network, no real GitHub.
"""
from contextlib import asynccontextmanager

from agent.mcp_client import MCPClient, _flatten_content, _looks_read_only


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, blocks, is_error=False):
        self.content = blocks
        self.isError = is_error


class _FakeSession:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return self._result


def _client(session=None, schemas=None):
    mc = MCPClient(url="http://x/mcp", token="tok", server_name="github")
    if session is not None:
        # Fresh-session-per-call: mark started and stub _open_session to yield the
        # fake, matching the real one-`async with`-per-call design.
        mc._started = True

        @asynccontextmanager
        async def _fake_open():
            yield session

        mc._open_session = _fake_open
    if schemas is not None:
        mc._schemas = schemas
    return mc


# ---------------------------------------------------------------- _flatten
def test_flatten_joins_text_blocks():
    assert _flatten_content(_Result([_Block("a"), _Block("b")])) == "a\nb"


def test_flatten_empty_is_placeholder():
    assert _flatten_content(_Result([])) == "(no content)"


def test_flatten_truncates_huge_output():
    out = _flatten_content(_Result([_Block("x" * 20000)]))
    assert len(out) < 20000 and "truncated" in out


# ---------------------------------------------------------------- namespacing
def test_owns_matches_only_this_servers_prefix():
    mc = _client()
    assert mc.owns("mcp__github__search_repositories")
    assert not mc.owns("save_project")
    assert not mc.owns("mcp__other__x")


def test_schemas_returns_a_copy():
    mc = _client(schemas=[{"type": "function", "function": {"name": "mcp__github__x"}}])
    grabbed = mc.schemas()
    grabbed.append("mutation")
    assert len(mc.schemas()) == 1  # internal list not affected


# ---------------------------------------------------------------- call routing
async def test_call_strips_prefix_and_flattens():
    fake = _FakeSession(_Result([_Block("repo data")]))
    mc = _client(session=fake)
    ok, text = await mc.call("mcp__github__get_repo", {"owner": "me", "repo": "x"})
    assert ok and text == "repo data"
    # the namespaced prefix is stripped before hitting the real MCP tool
    assert fake.calls == [("get_repo", {"owner": "me", "repo": "x"})]


async def test_call_none_args_becomes_empty_dict():
    fake = _FakeSession(_Result([_Block("ok")]))
    mc = _client(session=fake)
    await mc.call("mcp__github__list_repos", None)
    assert fake.calls == [("list_repos", {})]


async def test_call_reports_error_result():
    fake = _FakeSession(_Result([_Block("boom")], is_error=True))
    mc = _client(session=fake)
    ok, text = await mc.call("mcp__github__x", {})
    assert not ok and text == "boom"


async def test_call_when_not_connected_fails_soft():
    mc = _client(session=None)
    ok, text = await mc.call("mcp__github__x", {})
    assert not ok and "not connected" in text


# ---------------------------------------------------------------- read-only filter
def test_looks_read_only_classifies_verbs():
    for name in ("get_file_contents", "list_repositories", "search_code", "read_file"):
        assert _looks_read_only(name)
    for name in ("create_or_update_file", "merge_pull_request", "delete_file",
                 "push_files", "fork_repository", "add_issue_comment"):
        assert not _looks_read_only(name)
