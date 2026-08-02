#!/usr/bin/env python3
# ============================================================================
# careeragent-review - Dossier client (write reviews into the projects library)
# ============================================================================
#
# careeragent-review is a SECOND producer into careeragent-dossier's projects
# tier (careeragent-api is the first). It:
#   - reads the stored commit_sha for a repo (idempotency) via
#     GET /projects?external_id=owner/repo
#   - writes each repo's structured review via POST /projects (upsert by
#     external_id), including the reviewed commit_sha.
#
# Auth: X-API-Key (DOSSIER_API_KEY). Never raises on HTTP status — returns
# (status, body) so the harness decides.
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("careeragent-review")

Result = Tuple[int, Any]


class DossierClient:
    """Outbound client for careeragent-dossier projects endpoints."""

    def __init__(self, url: str, api_key: str, timeout: float = 15.0) -> None:
        if not url:
            raise ValueError("DossierClient.url is required")
        if not api_key:
            raise ValueError("DossierClient.api_key is required")
        self.url = url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(
            base_url=self.url,
            timeout=self._timeout,
            headers={"X-API-Key": self._api_key},
        )
        logger.info("DossierClient started (url=%s)", self.url)

    async def stop(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception as err:
            logger.warning("Error closing DossierClient: %s: %s", type(err).__name__, err)
        finally:
            self._http = None

    async def _request(self, method: str, path: str, **kw) -> Result:
        if self._http is None:
            return 0, {"error": "dossier client not started"}
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.HTTPError as err:
            return 0, {"error": f"{type(err).__name__}: {err}"}
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return resp.status_code, body

    async def get_by_external_id(self, external_id: str) -> Optional[dict]:
        """Return the stored project row for a repo (owner/repo), or None.
        Used to read commit_sha before deciding whether to re-review."""
        status, body = await self._request(
            "GET", "/projects", params={"external_id": external_id, "limit": 1}
        )
        if status == 200 and isinstance(body, list) and body:
            return body[0]
        return None

    async def save_project(self, fields: Dict[str, Any]) -> Result:
        """Upsert a project (POST /projects). Only non-None fields are sent so an
        existing populated row isn't blanked on refresh."""
        clean = {k: v for k, v in fields.items() if v is not None}
        return await self._request("POST", "/projects", json=clean)

    async def healthy(self) -> bool:
        """Probe dossier's /health so a dossier outage (which would fail every
        write) actually shows in this service's /health."""
        status, body = await self._request("GET", "/health")
        return status == 200 and (isinstance(body, dict) and body.get("status") in ("ok", "degraded"))
