#!/usr/bin/env python3
# ============================================================================
# careeragent-api - shared helpers for the subagent + compaction paths (P6)
# ============================================================================
#
# Tiny, pure helpers used by BOTH agent/subagents.py and agent/compaction.py.
# They live here (not in loop.py) so those two modules don't import loop.py —
# loop.py imports THEM, and a reverse import would be a cycle. Deliberately
# duplicates the shape of loop.py's private _extract_message / flatten so the
# nested paths stay decoupled from the main turn loop.
# ============================================================================

from __future__ import annotations

import json
from typing import Any, Dict, List


def extract_message(resp: Any) -> Dict[str, Any]:
    """Pull the assistant message out of a completion dict (mirror of loop._extract_message)."""
    if isinstance(resp, dict):
        try:
            msg = resp["choices"][0]["message"]
            if isinstance(msg, dict):
                return msg
        except (KeyError, IndexError, TypeError):
            pass
        if isinstance(resp.get("message"), dict):
            return resp["message"]
    return {}


def parse_args(raw: Any) -> Dict[str, Any]:
    """Coerce a tool call's `arguments` (a dict or a JSON string) into a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw or "{}")
            return v if isinstance(v, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def flatten_to_messages(system_content: str, convo: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A TEXT-ONLY message list for a tools-disabled call (mirror of loop._flatten_for_synthesis).

    Bedrock Converse rejects toolUse/toolResult blocks when no toolConfig is sent,
    so assistant tool_calls and tool-role results are flattened to plain text and
    the tool role is dropped. `convo` here is the message list AFTER the system
    message (i.e. pass convo[1:] or a slice)."""
    out: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]
    for m in convo:
        role = m.get("role")
        if role == "tool":
            out.append({"role": "user", "content": f"[tool result] {m.get('content', '')}"})
        elif role == "assistant":
            content = m.get("content") or ""
            calls = m.get("tool_calls") or []
            if calls:
                names = ", ".join((c.get("function") or {}).get("name", "?") for c in calls)
                content = f"{content}\n[called: {names}]".strip()
            out.append({"role": "assistant", "content": content or "(working…)"})
        elif role == "system":
            continue  # never carry a second system message into the flattened list
        else:
            out.append({"role": role, "content": m.get("content", "")})
    return out


def flatten_to_text(convo_slice: List[Dict[str, Any]]) -> str:
    """A readable single-string transcript of a convo slice — for the compaction
    summarizer (which takes the slice as untrusted DATA to summarize)."""
    lines: List[str] = []
    for m in convo_slice:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role == "assistant":
            calls = m.get("tool_calls") or []
            if calls:
                names = ", ".join((c.get("function") or {}).get("name", "?") for c in calls)
                content = (content + f" [called: {names}]").strip()
            lines.append(f"ASSISTANT: {content}" if content else "ASSISTANT: (tool call)")
        elif role == "tool":
            lines.append(f"TOOL RESULT: {content}")
        elif role == "user":
            lines.append(f"USER: {content}")
    return "\n".join(lines)
