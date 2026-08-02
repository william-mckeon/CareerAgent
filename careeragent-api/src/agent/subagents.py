#!/usr/bin/env python3
# ============================================================================
# careeragent-api - general subagent delegation (P6 #8): the lean sub-run
# ============================================================================
#
# spawn_subagent (intercepted in loop.py) calls run_subagent() — a small,
# NON-streaming, role-scoped tool loop that gives a narrow subtask its own clean
# context and returns ONLY its final text to the parent. Modeled on
# careeragent-review/src/harness/subagent.py::review_one.
#
# What it deliberately is NOT (and why the coach's run_agent is not reused):
#   * no SSE — it returns a string, not a byte stream;
#   * no ask_user / approval suspend — a child pausing would corrupt the single
#     P4 channel, so no role's catalog contains ask_user and every role is
#     read-only (approval never triggers);
#   * no sessions steering / interrupt — it never touches sessions;
#   * no P3 ledger / grounding / Guardian — a read-only child mints no verified
#     write, so there is nothing to gate and nothing that can launder a claim;
#   * no spawn_subagent — absent from every role catalog, so a child can't spawn
#     (the depth cap, enforced at the schema level).
# It reuses the SAME infra_client / dossier_client / fetch_client already in the
# parent's scope. Never raises to the caller (loop.py also guards).
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, List, Dict, Optional

from client.dossier import DossierClient
from client.infra import InfraClient

from . import permissions, roster, tools
from .subutil import extract_message, flatten_to_messages, parse_args

logger = logging.getLogger("careeragent-api")

DEFAULT_SUBAGENT_MAX_STEPS = 10
DEFAULT_SUBAGENT_EFFORT = "low"

_EMPTY_RESULT = "(the subagent produced no usable result.)"


def _finish_text(args: Dict[str, Any]) -> str:
    """The subagent's final text, from its finish_answer summary (+ any open items)."""
    summary = (args.get("summary") or "").rstrip()
    extra = args.get("open_items")
    if isinstance(extra, list) and extra:
        summary += "\n\nStill open:\n" + "\n".join(f"- {x}" for x in extra)
    return summary


async def _salvage(infra_client: InfraClient, system_content: str,
                   convo: List[Dict[str, Any]], effort: str) -> str:
    """Out of steps (or a blank reply): one TOOLS-DISABLED call to turn the work so
    far into a final answer. Never raises — returns a placeholder if it can't."""
    directive = (system_content + "\n\nYou are out of steps. Using ONLY what you have gathered, give "
                 "your best final answer now, concisely. Do not ask for more.")
    messages = flatten_to_messages(directive, convo[1:])
    payload: Dict[str, Any] = {"messages": messages, "tools": []}
    if effort:
        payload["reasoning_effort"] = effort
    try:
        resp = await infra_client.complete(payload)
        return (extract_message(resp).get("content") or "").strip() or _EMPTY_RESULT
    except Exception as err:
        logger.warning("subagent salvage failed: %s: %s", type(err).__name__, err)
        return _EMPTY_RESULT


async def run_subagent(
    *,
    task: str,
    role: str,
    infra_client: InfraClient,
    dossier_client: DossierClient,
    profile_content: str = "",
    fetch_client: Any = None,
    review_client: Any = None,
    code_client: Any = None,
    max_steps: int = DEFAULT_SUBAGENT_MAX_STEPS,
    effort: str = DEFAULT_SUBAGENT_EFFORT,
) -> str:
    """Run one role-scoped, read-only, non-streaming subagent; return only its
    final text. Never raises."""
    if not roster.is_role(role):
        return f"(unknown subagent role: {role})"
    task = (task or "").strip()
    if not task:
        return "(no task was provided to the subagent.)"

    system_content = roster.build_role_system(role, profile_content)
    allowed = roster.ROLE_TOOLSETS.get(role, set())
    schemas = roster.schemas_for_role(role)
    convo: List[Dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": task},
    ]

    for step in range(max_steps):
        payload: Dict[str, Any] = {"messages": convo, "tools": schemas, "tool_choice": "auto"}
        if effort:
            payload["reasoning_effort"] = effort
        try:
            resp = await infra_client.complete(payload)
        except Exception as err:
            logger.warning("subagent(%s): model call failed at step %d: %s: %s",
                           role, step, type(err).__name__, err)
            return _EMPTY_RESULT

        msg = extract_message(resp)
        tool_calls = msg.get("tool_calls") or []
        convo.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            **({"tool_calls": tool_calls} if tool_calls else {}),
        })

        if not tool_calls:
            # A plain reply IS the subagent's answer.
            content = (msg.get("content") or "").strip()
            return content or await _salvage(infra_client, system_content, convo, effort)

        finished: Optional[str] = None
        for tc in tool_calls:
            tc_id = (tc or {}).get("id")
            fn = (tc or {}).get("function") or {}
            name = fn.get("name", "")
            args = parse_args(fn.get("arguments"))

            if name == "finish_answer":
                finished = _finish_text(args)
                convo.append({"role": "tool", "tool_call_id": tc_id, "content": "acknowledged."})
                continue
            if name == "update_plan":
                convo.append({"role": "tool", "tool_call_id": tc_id, "content": "Plan noted."})
                continue

            # A tool call: enforce the role restriction + read-only by construction.
            # (allowed excludes read_profile/ask_user/spawn_subagent/writes already.)
            if name not in allowed or permissions.is_mutating(name):
                convo.append({"role": "tool", "tool_call_id": tc_id,
                              "content": f"You cannot use '{name}' in the {role} role. Use only your "
                                         "allowed read tools, then call finish_answer."})
                continue
            args, arg_err = tools.coerce_and_check(name, args)
            if arg_err:
                convo.append({"role": "tool", "tool_call_id": tc_id,
                              "content": f"Bad arguments — {arg_err}"})
                continue
            result = await tools.dispatch(name, args, dossier_client, review_client, fetch_client,
                                          code_client=code_client)
            convo.append({"role": "tool", "tool_call_id": tc_id, "content": result.content})

        if finished is not None:
            # An empty finish (weak model called finish_answer with a blank summary)
            # must not return bare "" — salvage the reads gathered so far, else the
            # sentinel, so the parent never mistakes silence for a clean result.
            return finished or await _salvage(infra_client, system_content, convo, effort)

    # Out of steps without finishing — salvage a final answer from the work done.
    logger.info("subagent(%s): hit max_steps=%d without finish — salvaging", role, max_steps)
    return await _salvage(infra_client, system_content, convo, effort)
