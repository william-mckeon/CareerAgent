"""
tests/test_permissions.py

The agent's write guardrail. Pure logic — no network, no clients.
"""
from agent import permissions


def test_read_tools_allowed_in_every_mode():
    for mode in ("plan", "default", "acceptEdits", "bypass"):
        assert permissions.decide("read_profile", mode).allowed
        assert permissions.decide("search_applications", mode).allowed
        assert permissions.decide("get_application", mode).allowed


def test_plan_mode_blocks_writes():
    d = permissions.decide("save_resume", "plan")
    assert not d.allowed
    assert "read-only" in d.reason.lower()


def test_default_mode_blocks_writes():
    assert not permissions.decide("edit_profile", "default").allowed


def test_accept_edits_allows_writes():
    assert permissions.decide("save_resume", "acceptEdits").allowed
    assert permissions.decide("edit_profile", "acceptEdits").allowed
    assert permissions.decide("create_application", "acceptEdits").allowed


def test_delete_needs_confirmation_even_in_accept_edits():
    d = permissions.decide("delete_application", "acceptEdits")
    assert not d.allowed
    assert "destructive" in d.reason.lower() or "confirm" in d.reason.lower()


def test_bypass_allows_everything_including_delete():
    assert permissions.decide("delete_application", "bypass").allowed
    assert permissions.decide("save_resume", "bypass").allowed


def test_unknown_mode_normalizes_to_default_and_blocks_writes():
    assert permissions.normalize_mode("wat") == "default"
    assert not permissions.decide("save_resume", "wat").allowed


# ---------------------------------------------------------------- MCP tools
def test_mcp_read_tools_allowed_in_every_mode():
    for mode in ("plan", "default", "acceptEdits", "bypass"):
        assert permissions.decide("mcp__github__get_file_contents", mode).allowed
        assert permissions.decide("mcp__github__list_repositories", mode).allowed
        assert permissions.decide("mcp__github__search_code", mode).allowed


def test_mcp_write_tools_blocked_in_read_only_modes():
    # The HIGH review finding: GitHub WRITE tools must NOT be allowed in plan /
    # default — the guardrail can't depend on the PAT scope.
    for tool in ("mcp__github__create_or_update_file",
                 "mcp__github__merge_pull_request",
                 "mcp__github__delete_file"):
        assert not permissions.decide(tool, "plan").allowed
        assert not permissions.decide(tool, "default").allowed
        # acceptEdits would permit them (but the client filters them out anyway)
        assert permissions.decide(tool, "acceptEdits").allowed


def test_review_repos_is_mutating():
    # review_repos triggers downstream writes into the projects library — gate it
    # like a write (plan/default deny; acceptEdits allows).
    assert not permissions.decide("review_repos", "plan").allowed
    assert not permissions.decide("review_repos", "default").allowed
    assert permissions.decide("review_repos", "acceptEdits").allowed
