#!/usr/bin/env python3
# ============================================================================
# careeragent-api - Render client (outbound to careeragent-render)
# ============================================================================
#
# Backs the agent's `render_resume` WRITE tool. Modeled on AtsClient/FetchClient:
# owns its own httpx.AsyncClient with X-API-Key pre-attached, never raises on HTTP
# status (returns (status, body) so tools.dispatch formats it).
#
# careeragent-render is a PURE, STATELESS renderer — markdown résumé in, PDF/DOCX
# bytes out (base64) — no model, no DB, no egress. The api resolves the SAVED
# résumé from dossier, hands it here, gets bytes back, and persists them in dossier
# (the bytes never ride a tool result or the /chat SSE content stream). reportlab
# layout is CPU work; careeragent-render bounds it (per-block cap + a hard render
# deadline BELOW this read timeout), so a render returns a clean status here rather
# than a ReadTimeout.
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

import httpx

logger = logging.getLogger("careeragent-api")

Result = Tuple[int, Any]


class RenderClient:
    """Outbound HTTP client for careeragent-render. Config-only construction;
    start()/stop() managed by the FastAPI lifespan. Never raises on status."""

    def __init__(self, url: str, api_key: str, read_timeout: float = 30.0) -> None:
        if not url:
            raise ValueError("RenderClient.url is required")
        if not api_key:
            raise ValueError("RenderClient.api_key is required")
        self.url = url.rstrip("/")
        self._api_key = api_key
        self._read_timeout = read_timeout
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(connect=5.0, read=self._read_timeout, write=15.0, pool=5.0),
            headers={"X-API-Key": self._api_key},
        )
        logger.info("RenderClient started (url=%s, read_timeout=%ss)", self.url, self._read_timeout)

    async def stop(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception as err:
            logger.warning("Error closing RenderClient: %s: %s", type(err).__name__, err)
        finally:
            self._http = None

    async def render(self, resume: str, fmt: str, title: Optional[str] = None) -> Result:
        """POST /render — hand careeragent-render the résumé markdown + target format and
        get back `{content_b64, format, bytes, filename}` (200), or a `{detail}` error
        (400 empty/bad-format, 413 oversize, 4xx/5xx). Returns (status, body). Never raises."""
        if self._http is None:
            return 0, {"detail": "render client not started"}
        payload = {"resume": resume or "", "format": fmt}
        if title:
            payload["title"] = title
        try:
            resp = await self._http.post("/render", json=payload)
        except httpx.HTTPError as err:
            return 0, {"detail": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
