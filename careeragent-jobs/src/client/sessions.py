#!/usr/bin/env python3
# ============================================================================
# careeragent-jobs - careeragent-sessions client (result injection)
# ============================================================================
#
# When a background job finishes, the worker INJECTS its result as an assistant
# message into the job's conversation so the user sees it appear ("do not poll").
# That is this client's one job: POST the finished summary to
# careeragent-sessions' inject endpoint.
#
# Owns its own httpx.AsyncClient with X-API-Key pre-attached; start()/stop() are
# managed by the FastAPI lifespan. Never raises on status (returns (status,
# body)); a transport error surfaces as (0, {"error": ...}). Injection is
# best-effort — the job is already marked 'done' before this is attempted.
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

import httpx

logger = logging.getLogger("careeragent-jobs")

Result = Tuple[int, Any]


class SessionsClient:
    """Outbound HTTP client for careeragent-sessions' inject endpoint."""

    def __init__(self, url: str, api_key: str, timeout: float = 10.0) -> None:
        if not url:
            raise ValueError("SessionsClient.url is required")
        if not api_key:
            raise ValueError("SessionsClient.api_key is required")
        self.url = url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(connect=5.0, read=self._timeout, write=5.0, pool=5.0),
            headers={"X-API-Key": self._api_key},
        )
        logger.info("SessionsClient started (url=%s)", self.url)

    async def stop(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception as err:
            logger.warning("Error closing SessionsClient: %s: %s", type(err).__name__, err)
        finally:
            self._http = None

    async def inject(self, conversation_id: str, role: str, content: str) -> Result:
        """POST /conversations/{conversation_id}/inject with {"role", "content"}.
        Returns (status, body). Never raises; contract: 200 on success, 404 if
        the conversation doesn't exist."""
        if self._http is None:
            return 0, {"error": "sessions client not started"}
        try:
            resp = await self._http.post(
                f"/conversations/{conversation_id}/inject",
                json={"role": role, "content": content},
            )
        except httpx.HTTPError as err:
            return 0, {"error": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data

    async def create_conversation(self, title: str) -> Result:
        """POST /conversations to mint the singleton "🔔 Reminders" conversation.
        Returns (status, body); body carries {"conversation_id": <uuid>} on 200."""
        if self._http is None:
            return 0, {"error": "sessions client not started"}
        try:
            resp = await self._http.post("/conversations", json={"title": title})
        except httpx.HTTPError as err:
            return 0, {"error": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data

    async def get_conversation(self, conversation_id: str) -> Result:
        """GET /conversations/{id} — used only to check the persisted Reminders
        conversation still EXISTS (200) before reusing it, so a user who deleted
        it gets a fresh one instead of a 404 on every injected reminder."""
        if self._http is None:
            return 0, {"error": "sessions client not started"}
        try:
            resp = await self._http.get(f"/conversations/{conversation_id}")
        except httpx.HTTPError as err:
            return 0, {"error": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data

    async def list_conversations(self, limit: int = 200) -> Result:
        """GET /conversations — used to RECONCILE the singleton Reminders thread by
        title (self-healing dedup) before minting a new one, so a create whose
        response was lost / not persisted doesn't fork a second thread. Returns
        (status, body) where body is the JSON list."""
        if self._http is None:
            return 0, {"error": "sessions client not started"}
        try:
            resp = await self._http.get("/conversations", params={"limit": limit})
        except httpx.HTTPError as err:
            return 0, {"error": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
