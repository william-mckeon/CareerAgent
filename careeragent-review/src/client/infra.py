#!/usr/bin/env python3
# ============================================================================
# careeragent-review - Infra client (the model gateway for review subagents)
# ============================================================================
#
# Each per-repo review subagent runs a bounded tool-calling loop against
# careeragent-infra's /complete endpoint (non-streaming, tool-aware). This is a
# focused copy of careeragent-api's InfraClient — only complete() + health are
# needed here (review never streams to a user).
#
# Auth: X-API-Key (INFRA_API_KEY), pre-attached. Never logged.
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("careeragent-review")


class InfraClient:
    """Outbound client for careeragent-infra /complete."""

    def __init__(
        self,
        url: str,
        api_key: str,
        connect_timeout: float = 10.0,
        read_timeout: Optional[float] = 600.0,
    ) -> None:
        if not url:
            raise ValueError("InfraClient.url is required")
        if not api_key:
            raise ValueError("InfraClient.api_key is required")
        self.url = url.rstrip("/")
        self._api_key = api_key
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(
                connect=self._connect_timeout, read=self._read_timeout, write=10.0, pool=5.0
            ),
            headers={"X-API-Key": self._api_key},
        )
        logger.info("InfraClient started (url=%s, read_timeout=%ss)", self.url, self._read_timeout)

    async def stop(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception as err:
            logger.warning("Error closing InfraClient: %s: %s", type(err).__name__, err)
        finally:
            self._http = None

    async def complete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /complete → parsed OpenAI-style completion dict. Raises on
        transport error / non-2xx (the subagent loop catches it)."""
        if self._http is None:
            raise RuntimeError("InfraClient.complete() called before start().")
        resp = await self._http.post("/complete", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def healthy(self) -> bool:
        if self._http is None:
            return False
        try:
            resp = await self._http.get("/health", timeout=5.0)
            return resp.status_code == 200 and (resp.json() or {}).get("status") == "ok"
        except Exception:
            return False
