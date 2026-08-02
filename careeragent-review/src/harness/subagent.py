#!/usr/bin/env python3
# ============================================================================
# careeragent-review - per-repo review subagent (one bounded tool-calling loop)
# ============================================================================
#
# review_one() runs a fresh, isolated tool-calling loop for ONE repo against
# careeragent-infra /complete, using the GitHub MCP read tools + the terminal
# `submit_review` tool. It is the OpenCode "bounded child agent" — its own
# context, capped at max_steps, returning a small structured dict. It reads no
# files itself; the model drives the reads through the MCP tools.
#
# Fresh context per repo = the whole point: many repos reviewed without any one
# of them blowing a shared context window.
# ============================================================================

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from client.infra import InfraClient
from client.mcp_client import MCPClient

from .prompts import REVIEW_FIELDS, REVIEWER_SYSTEM_PROMPT, SUBMIT_REVIEW_TOOL, build_task

logger = logging.getLogger("careeragent-review")


def _parse_args(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw or "{}")
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


def _clean_review(args: Dict[str, Any]) -> Dict[str, Any]:
    """Allowlist submit_review args to the dossier columns, dropping blanks. The
    ONLY int-typed column is `stars`; a model may emit "1.2k"/"1,200" despite the
    integer schema (Bedrock doesn't hard-enforce tool-arg types), which would 422
    the whole dossier write and discard a completed review — so coerce or drop it."""
    out = {k: args[k] for k in REVIEW_FIELDS if args.get(k) not in (None, "")}
    s = out.get("stars")
    if s is not None and (isinstance(s, bool) or not isinstance(s, int)):
        try:
            out["stars"] = int(str(s).replace(",", "").strip())
        except (ValueError, TypeError):
            out.pop("stars", None)
    return out


def _submitted_review(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the cleaned submit_review args if this assistant turn submitted, else None."""
    for tc in (msg.get("tool_calls") or []):
        fn = (tc or {}).get("function") or {}
        if fn.get("name") == "submit_review":
            return _clean_review(_parse_args(fn.get("arguments")))
    return None


async def _force_submit(
    repo_full_name: str,
    messages: List[Dict[str, Any]],
    *,
    infra: InfraClient,
    model: str,
    reasoning_effort: str,
) -> Optional[Dict[str, Any]]:
    """Last-ditch SALVAGE when the reviewer ran out of steps without submitting.

    One more call with submit_review as the ONLY tool and an explicit directive to
    submit now — so a repo the model actually read still yields a structured review
    instead of vanishing (the review-side analog of the coach's synthesis turn).
    Uses tool_choice='auto' (not a forced toolChoice) to avoid any Bedrock/gpt-oss
    forced-tool incompatibility; if the model still won't submit, we return None —
    no worse than the old behavior. Never raises."""
    messages = messages + [{"role": "user", "content":
        "You are out of research steps — do NOT read anything else. Call submit_review NOW with your "
        "best assessment from what you have already seen. Leave any field you could not determine "
        "empty; a partial review is fine. Returning without submitting is not allowed."}]
    payload = {
        "messages": messages,
        "tools": [SUBMIT_REVIEW_TOOL],
        "tool_choice": "auto",
        "model": model,
        "reasoning_effort": reasoning_effort,
    }
    try:
        resp = await infra.complete(payload)
        msg = resp["choices"][0]["message"]
    except Exception as err:
        logger.warning("review(%s): salvage submit call failed: %s", repo_full_name, err)
        return None
    salvaged = _submitted_review(msg) if isinstance(msg, dict) else None
    if salvaged is not None:
        logger.info("review(%s): salvaged a partial review on the final step", repo_full_name)
    return salvaged


async def review_one(
    repo_full_name: str,
    *,
    focus: Optional[str],
    infra: InfraClient,
    mcp: MCPClient,
    max_steps: int = 12,
    model: str = "base",
    reasoning_effort: str = "low",
) -> Optional[Dict[str, Any]]:
    """Review one repo. Returns the submit_review fields (dict, allowlisted to
    REVIEW_FIELDS) or None if the model never submitted within max_steps.

    Raises on infra transport failure — the orchestrator catches per-repo so one
    repo's model error never fails the whole batch."""
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
        {"role": "user", "content": build_task(repo_full_name, focus)},
    ]
    tools = mcp.schemas() + [SUBMIT_REVIEW_TOOL]

    for step in range(max_steps):
        payload = {
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "model": model,
            "reasoning_effort": reasoning_effort,
        }
        resp = await infra.complete(payload)
        try:
            msg = resp["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            logger.warning("review(%s): malformed completion at step %d", repo_full_name, step)
            return None

        # Append the assistant turn verbatim (carries tool_calls / null content).
        messages.append(msg)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            # Model answered without submitting — nothing structured to store.
            return None

        # If the model submitted this turn, that's the answer — take it and stop.
        submitted = _submitted_review(msg)
        if submitted is not None:
            return submitted

        # Otherwise this is a read turn — execute EVERY tool_call and feed the
        # results back (the model contract needs one result per tool_call_id).
        for tc in tool_calls:
            tc_id = (tc or {}).get("id")
            fn = (tc or {}).get("function") or {}
            name = fn.get("name", "")
            args = _parse_args(fn.get("arguments"))
            if mcp.owns(name):
                _, text = await mcp.call(name, args)
            else:
                text = f"(unknown tool: {name})"
            messages.append({"role": "tool", "tool_call_id": tc_id, "content": text})

    # Out of steps without a submit — salvage one partial review rather than drop
    # the repo silently (observed live: certain repos always exhaust the budget).
    # Run the salvage at AT LEAST medium effort regardless of the reviewer's effort:
    # a low-effort forced-submit was observed to still refuse to submit, and this is
    # a single rare call, so the extra reasoning is worth the compliance.
    salvage_effort = reasoning_effort if reasoning_effort in ("medium", "high") else "medium"
    logger.info("review(%s): hit max_steps=%d without submit_review — attempting salvage (effort=%s)",
                repo_full_name, max_steps, salvage_effort)
    return await _force_submit(repo_full_name, messages,
                               infra=infra, model=model, reasoning_effort=salvage_effort)
