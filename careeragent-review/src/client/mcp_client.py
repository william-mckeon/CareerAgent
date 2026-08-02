#!/usr/bin/env python3
# ============================================================================
# careeragent-review - MCP client (streamable-HTTP link to careeragent-github-mcp)
# ============================================================================
#
# A per-repo review subagent reads the user's GitHub repo through the GitHub
# MCP server (careeragent-github-mcp) over streamable HTTP. This client is a
# faithful copy of careeragent-api/src/agent/mcp_client.py — services share no
# code across repos, so the module is VENDORED here (the MCP client is a code
# module, not an HTTP endpoint).
#
# ── TASK-SAFETY (why we never hold a session open) ──────────────────────────
# The mcp streamable-HTTP client is built on anyio task groups / cancel scopes
# that MUST be entered and exited in the SAME asyncio task. The review harness
# fans out with asyncio.gather, so each per-repo subagent runs in its own task
# and each tool call must open/close its own session inside that task. So this
# client holds NOTHING open: every call opens and closes its own session within
# one `async with`. A single shared MCPClient instance is therefore gather-safe.
#
# Connects PAT-LESS: careeragent-github-mcp (Caddy) injects the Authorization
# header, so this service holds no GitHub credential.
# ============================================================================

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger("careeragent-review")

# Cap on a single MCP tool result fed back to the model. GitHub tools can return
# whole files / trees; without a cap one call could blow the context window.
_MAX_RESULT_CHARS = 6000

# Per-call timeout so a hung/slow GitHub tool can't stall a subagent forever.
_CALL_TIMEOUT_SECONDS = 60.0

# Read-verb prefixes: an MCP tool whose name starts with one of these is treated
# as read-only. Everything else is filtered out of the catalog when read_only is
# set — review never needs (or is allowed) write tools.
_READ_VERBS = ("get", "list", "search", "read")


def _looks_read_only(tool_name: str) -> bool:
    return tool_name.lower().startswith(_READ_VERBS)


def _flatten_content(result: Any, max_chars: int = _MAX_RESULT_CHARS) -> str:
    """Turn an MCP CallToolResult's content blocks into a single text string.
    Control calls (head sha, enumeration) pass a large max_chars so truncation
    never breaks their JSON parse."""
    parts: List[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    if not parts:
        structured = getattr(result, "structuredContent", None)
        if structured:
            try:
                parts.append(json.dumps(structured, default=str))
            except Exception:
                parts.append(str(structured))
    out = "\n".join(parts) if parts else "(no content)"
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n…(truncated, {len(out)} chars total)"
    return out


class MCPClient:
    """Streamable-HTTP MCP client for one server (default: GitHub), read-only."""

    def __init__(
        self,
        url: str,
        token: Optional[str] = None,
        server_name: str = "github",
        read_only: bool = True,
    ) -> None:
        if not url:
            raise ValueError("MCPClient.url is required")
        self._url = url
        self._token = token
        self._server = server_name
        self._read_only = read_only
        self._started = False
        self._schemas: List[Dict[str, Any]] = []

    @property
    def prefix(self) -> str:
        return f"mcp__{self._server}__"

    @asynccontextmanager
    async def _open_session(self) -> AsyncIterator[Any]:
        """Open a fresh MCP session for ONE `async with`, in the caller's task."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
        async with streamablehttp_client(self._url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def start(self) -> None:
        """Connect once to discover + cache the tool catalog. Holds nothing open."""
        async with self._open_session() as session:
            listed = await session.list_tools()
        kept, filtered = [], 0
        for t in getattr(listed, "tools", []):
            if self._read_only and not _looks_read_only(getattr(t, "name", "")):
                filtered += 1
                continue
            kept.append(t)
        self._schemas = [
            {
                "type": "function",
                "function": {
                    "name": self.prefix + t.name,
                    "description": (getattr(t, "description", "") or "")[:1024],
                    "parameters": getattr(t, "inputSchema", None) or {
                        "type": "object", "properties": {},
                    },
                },
            }
            for t in kept
        ]
        self._started = True
        logger.info(
            "MCPClient connected (server=%s, url=%s): %d tools exposed "
            "(%d write tools filtered; read_only=%s)",
            self._server, self._url, len(self._schemas), filtered, self._read_only,
        )

    async def stop(self) -> None:
        self._started = False
        self._schemas = []

    def schemas(self) -> List[Dict[str, Any]]:
        return list(self._schemas)

    @property
    def started(self) -> bool:
        return self._started

    def owns(self, tool_name: str) -> bool:
        return tool_name.startswith(self.prefix)

    async def call(
        self, tool_name: str, args: Optional[Dict[str, Any]], max_chars: int = _MAX_RESULT_CHARS
    ) -> Tuple[bool, str]:
        """Execute a namespaced MCP tool call in a fresh session. Returns
        (ok, text). Never raises. `max_chars` caps the returned text — control
        calls (head sha, enumeration) pass a large value so JSON isn't truncated."""
        if not self._started:
            return False, "the GitHub tools are not connected right now."
        real_name = tool_name[len(self.prefix):] if self.owns(tool_name) else tool_name
        try:
            async with asyncio.timeout(_CALL_TIMEOUT_SECONDS):
                async with self._open_session() as session:
                    result = await session.call_tool(real_name, arguments=args or {})
        except asyncio.TimeoutError:
            return False, f"GitHub tool '{real_name}' timed out after {_CALL_TIMEOUT_SECONDS:g}s."
        except Exception as err:
            return False, f"MCP tool error: {type(err).__name__}: {err}"
        ok = not bool(getattr(result, "isError", False))
        return ok, _flatten_content(result, max_chars)
