#!/usr/bin/env python3
# ============================================================================
# careeragent-api - MCP client (the agent's first Model Context Protocol link)
# ============================================================================
#
# Connects the coach to an MCP server over STREAMABLE HTTP, discovers its tools,
# and exposes them to the agent loop namespaced as ``mcp__<server>__<tool>`` —
# so MCP tools sit in the model's tool catalog beside the built-in dossier
# tools, indistinguishable to the loop except that their calls route here.
#
# First (and, for now, only) server: GitHub, via the careeragent-github-mcp
# service, so the coach can review the user's repos to fill the projects library.
#
# ── TASK-SAFETY (why we never hold a session open) ──────────────────────────
# The mcp streamable-HTTP client is built on anyio task groups / cancel scopes,
# which MUST be entered and exited in the SAME asyncio task. A FastAPI app runs
# startup, each request, and shutdown in DIFFERENT tasks, so holding one
# ClientSession open across the app lifespan and calling it from request tasks
# raises "Attempted to exit cancel scope in a different task than it was entered
# in" and crashes the process. So this client holds NOTHING open: every use —
# tool discovery in start(), and each tool call — opens and closes its own
# session entirely within one `async with`, in the caller's task. The cost is a
# fresh connect+initialize per call (an extra round-trip); at the coach's tool
# cadence that is a fine trade for correctness.
#
# SAFETY: read-only. The server runs with --read-only, we also filter write
# tools out of the catalog here, and the permission engine treats non-read
# mcp__ tools as mutating — three independent guards.
# ============================================================================

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger("careeragent-api")

# Cap on a single MCP tool result fed back to the model. GitHub tools can return
# whole files / repo trees; without a cap one call could blow the context window.
_MAX_RESULT_CHARS = 6000

# Per-call timeout so a hung/slow GitHub tool can't stall the loop forever.
_CALL_TIMEOUT_SECONDS = 60.0

# Read-verb prefixes: an MCP tool whose name starts with one of these is treated
# as read-only. Everything else is a write tool and is filtered OUT of the
# catalog when read_only is set. (The permission engine independently classifies
# non-read mcp__ tools as mutating — defense-in-depth, not the only guard.)
_READ_VERBS = ("get", "list", "search", "read")


def _looks_read_only(tool_name: str) -> bool:
    return tool_name.lower().startswith(_READ_VERBS)


def _flatten_content(result: Any) -> str:
    """Turn an MCP CallToolResult's content blocks into a single text string."""
    parts: List[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    # No text blocks — fall back to structuredContent (some tools return only
    # structured data) so a real result isn't reported as "(no content)".
    if not parts:
        structured = getattr(result, "structuredContent", None)
        if structured:
            try:
                parts.append(json.dumps(structured, default=str))
            except Exception:
                parts.append(str(structured))
    out = "\n".join(parts) if parts else "(no content)"
    if len(out) > _MAX_RESULT_CHARS:
        out = out[:_MAX_RESULT_CHARS] + f"\n…(truncated, {len(out)} chars total)"
    return out


class MCPClient:
    """Streamable-HTTP MCP client for one server (default: GitHub).

    ``start()`` connects once to discover + cache tool schemas (namespaced);
    ``schemas()`` returns them for the model; ``owns(name)`` / ``call(name,
    args)`` route a tool call. Holds nothing open between calls (see task-safety
    note above). Fails soft: any error leaves the client unstarted so the agent
    runs without it.
    """

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
        """Open a fresh MCP session for the duration of ONE `async with`, in the
        CALLER's task, and close it on exit. Never held across tasks."""
        # Lazy import so the module (and the whole agent) imports even when the
        # `mcp` SDK isn't installed — MCP is opt-in.
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
        async with streamablehttp_client(self._url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def start(self) -> None:
        """Connect once to discover + cache the tool catalog (and verify
        connectivity). Holds nothing open. Raises on failure — the caller
        (lifespan) catches it and leaves the agent running without MCP."""
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
        # Nothing is held open; just drop cached state.
        self._started = False
        self._schemas = []

    # ------------------------------------------------------------ the surface
    def schemas(self) -> List[Dict[str, Any]]:
        """OpenAI function schemas for the discovered tools (namespaced). Empty
        until start() succeeds."""
        return list(self._schemas)

    def owns(self, tool_name: str) -> bool:
        return tool_name.startswith(self.prefix)

    async def call(self, tool_name: str, args: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
        """Execute a namespaced MCP tool call in a fresh session. Returns
        (ok, text). Never raises — errors come back as (False, message) so the
        loop keeps making progress."""
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
        return ok, _flatten_content(result)
