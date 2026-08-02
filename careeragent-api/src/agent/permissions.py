#!/usr/bin/env python3
# ============================================================================
# careeragent-api - Permission engine (the agent's write guardrail)
# ============================================================================
#
# Ported and simplified from openagent-code's permission model. It gates every
# tool call the coach wants to make, once, at dispatch — so a read-only
# "critique my resume" session physically cannot mutate anything, and a
# "rewrite it" session can.
#
# The two knobs are the MODE (per request/session) and whether a tool MUTATES:
#
#   plan        read-only. Every mutating tool is denied. ("critique, hands off")
#   acceptEdits read + write. Mutating tools allowed, EXCEPT destructive ones
#               (delete) which still require an explicit confirmation.
#   bypass      everything allowed (headless/automation).
#   default     like plan for writes — a mutating tool needs approval, so
#               without an interactive confirmation channel it is denied and the
#               model is told to ask the user to switch to acceptEdits.
#
# There is no filesystem "fence" here (unlike openagent-code): the ONLY tools
# that exist are dossier tools scoped to this user's data, so "can't touch
# anything outside the workspace" is satisfied by construction — there is no
# shell, no file access, no arbitrary path. The meaningful guardrail at this
# layer is the read/write mode gate.
# ============================================================================

from __future__ import annotations

from dataclasses import dataclass

VALID_MODES = ("plan", "acceptEdits", "bypass", "default")

# Tools that CHANGE state. Everything not listed is read-only and always
# allowed (past the mode check below). Keep in sync with agent/tools.py.
MUTATING = {
    "save_profile",
    "edit_profile",
    "create_application",
    "update_application",
    "delete_application",
    "add_contact",
    "save_resume",
    "edit_resume",
    "save_project",
    "update_project",
    "delete_project",
    "review_repos",
    "remember",
    "render_resume",
}

# Destructive tools that need an explicit confirmation even when writes are
# allowed. Only bypass mode auto-approves them.
DESTRUCTIVE = {"delete_application", "delete_project"}

# NOTE (P6): spawn_subagent is a CONTROL tool (intercepted in agent/loop.py, like
# finish_answer/ask_user) and is deliberately NOT listed as MUTATING — it delegates
# to READ-ONLY subagents that return text and change nothing, so it is allowed in
# every mode (incl. plan). Do NOT "fix" it into MUTATING: that would break read-only
# delegation. The child's read-only clamp is enforced by roster.schemas_for_role
# (the child catalog has no write tool) and by run_subagent, not here.


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    # allowed is False but the user could approve it in-chat (P4). The loop pauses
    # for a yes/no instead of hard-denying. A plain hard deny (e.g. plan mode) has
    # needs_approval=False.
    needs_approval: bool = False


def normalize_mode(mode: str) -> str:
    """Coerce an unknown/blank mode to the safe default."""
    return mode if mode in VALID_MODES else "default"


# MCP tool names are namespaced `mcp__<server>__<subtool>`. Only the read verbs
# are considered safe (non-mutating); ANY other MCP tool is treated as mutating,
# so read-only modes (plan/default) deny it. This is a write guardrail that does
# NOT rely on the PAT scope — the permission engine, not an external token, is
# the source of truth for "can this session change anything".
_MCP_READ_PREFIXES = ("get", "list", "search", "read")


def _mcp_is_read_only(tool_name: str) -> bool:
    subtool = tool_name.split("__", 2)[-1].lower()
    return subtool.startswith(_MCP_READ_PREFIXES)


def is_mutating(tool_name: str) -> bool:
    if tool_name in MUTATING:
        return True
    # An MCP tool is mutating unless its sub-name is a known read verb.
    if tool_name.startswith("mcp__"):
        return not _mcp_is_read_only(tool_name)
    return False


def decide(tool_name: str, mode: str, granted: bool = False) -> Decision:
    """Allow, deny, or REQUIRE-APPROVAL for a single tool call under the given mode.

    Read-only tools are always allowed. Mutating tools depend on the mode. `granted`
    is a one-shot, per-call approval the user just gave in-chat (P4) — it allows the
    exact call that was approved. The reason is fed back to the model as a teaching
    signal on a hard deny.
    """
    mode = normalize_mode(mode)

    if mode == "bypass":
        return Decision(True, "bypass mode")

    if not is_mutating(tool_name):
        return Decision(True, "read-only tool")

    # --- mutating tool from here on ---
    # A grant the user just approved for THIS call wins over the mode gate.
    if granted:
        return Decision(True, "approved by the user")

    if mode == "plan":
        # Read-only by design — a hard deny, not an approval prompt.
        return Decision(
            False,
            "plan mode is read-only — I can analyze but not change anything. "
            "Ask the user to switch to edit mode to make this change.",
        )

    if tool_name in DESTRUCTIVE:
        # Destructive always needs an explicit yes — even in acceptEdits.
        return Decision(
            False,
            "deleting is destructive and needs the user's explicit confirmation.",
            needs_approval=True,
        )

    if mode == "acceptEdits":
        return Decision(True, "acceptEdits mode allows writes")

    # default mode: a non-destructive write is fine WITH the user's approval —
    # pause and ask (no more "enable edit mode" dead-end).
    return Decision(
        False,
        "this change needs the user's approval.",
        needs_approval=True,
    )
