#!/usr/bin/env python3
# ============================================================================
# careeragent-api - careeragent-sessions client (P4.5 mid-run steering)
# ============================================================================
#
# The coach is stateless per request, but a running turn needs to notice
# steering messages / an interrupt the user posts to careeragent-sessions WHILE
# it runs. Between steps the loop calls drain_steer() here, which pulls (and
# clears) this run's queued steering + interrupt flag from sessions.
#
# FAIL-OPEN by design: steering is best-effort. Any error (sessions down, a
# timeout, a bad shape) returns "nothing queued, not interrupted" so a hiccup in
# an optional feature can never break or stall the actual turn.
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("careeragent-api")

_EMPTY: Dict[str, Any] = {"messages": [], "interrupted": False}


class SessionsClient:
    """Thin async client for careeragent-sessions' internal drain endpoint."""

    def __init__(self, url: str, api_key: str, timeout: float = 3.0) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(
            base_url=self._url,
            timeout=httpx.Timeout(connect=2.0, read=self._timeout, write=2.0, pool=2.0),
            headers={"X-API-Key": self._api_key},
        )

    async def stop(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            finally:
                self._http = None

    async def drain_steer(self, conversation_id: str) -> Dict[str, Any]:
        """Return {messages: [str], interrupted: bool} for this run, clearing both
        server-side. Never raises — any failure yields the empty result."""
        if self._http is None or not conversation_id:
            return dict(_EMPTY)
        try:
            resp = await self._http.post(f"/conversations/{conversation_id}/drain-steer")
            if resp.status_code != 200:
                return dict(_EMPTY)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as err:
            logger.debug("sessions drain_steer failed (ignored): %s", err)
            return dict(_EMPTY)
        if not isinstance(body, dict):
            return dict(_EMPTY)
        msgs = body.get("messages")
        messages: List[str] = [str(m) for m in msgs] if isinstance(msgs, list) else []
        return {"messages": messages, "interrupted": bool(body.get("interrupted"))}
