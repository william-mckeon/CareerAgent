#!/usr/bin/env python3
# ============================================================================
# careeragent-api - Dossier Client
# Maintainer: William McKeon
# Outbound HTTP client for the careeragent-dossier career-data store
# ============================================================================
#
# ROLE:
#   DossierClient encapsulates the outbound HTTP boundary from careeragent-api
#   (the agent) to careeragent-dossier. It is a sibling of InfraClient /
#   LoggerClient / MemoryClient and follows the same shape: owns its own
#   httpx.AsyncClient, owns its timeout + X-API-Key, exposes start()/stop(),
#   and presents one typed method per dossier tool endpoint.
#
#   The agent's tool layer (src/agent/tools.py) calls these methods to execute
#   the model's tool calls. Every method returns a (status_code, body) tuple —
#   it NEVER raises on an HTTP error status — so the tool layer can turn a
#   4xx/5xx into a teaching message the model can react to, exactly like an
#   exact-match edit failure. Only genuine transport errors (connect/timeout)
#   raise, and the tool layer catches those too.
#
#   Transport-only: no agent logic, no permission checks, no persona. It just
#   speaks HTTP to dossier. See careeragent-dossier/specs/0001-dossier.md for
#   the endpoint contract.
# ============================================================================

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("careeragent-api")

# One (status_code, parsed_body) pair. body is a dict/list on success, or a
# dict like {"detail": "..."} on an error status.
Result = Tuple[int, Any]


class DossierClient:
    """Outbound HTTP client for careeragent-dossier (the career-data store).

    Construction is pure config; no I/O until start(). start()/stop() are
    idempotent and match the lifecycle surface of the other client modules.
    """

    def __init__(self, url: str, api_key: str, timeout: float = 15.0) -> None:
        if not url:
            raise ValueError("DossierClient.url is required (got empty string).")
        if not api_key:
            raise ValueError("DossierClient.api_key is required (got empty string).")
        self.url: str = url.rstrip("/")
        self._api_key: str = api_key
        self._timeout: float = timeout
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(self._timeout),
            headers={"X-API-Key": self._api_key},
        )
        logger.info(f"DossierClient started (url={self.url}, timeout={self._timeout}s)")

    async def stop(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
            logger.info("DossierClient closed.")
        except Exception as err:
            logger.warning(f"Error closing DossierClient: {type(err).__name__}: {err}")
        finally:
            self._http = None

    async def aclose(self) -> None:
        await self.stop()

    async def ping(self) -> bool:
        """True if dossier's /health reports database ok (used by /health proxy)."""
        if self._http is None:
            return False
        try:
            r = await self._http.get("/health", timeout=5.0)
            return r.status_code == 200 and r.json().get("database") == "ok"
        except Exception:
            return False

    # ------------------------------------------------------------------ core
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Result:
        if self._http is None:
            raise RuntimeError("DossierClient used before start().")
        resp = await self._http.request(method, path, json=json, params=params)
        try:
            body = resp.json()
        except Exception:
            body = {"detail": resp.text or "<no body>"}
        return resp.status_code, body

    # ------------------------------------------------------------- profile
    async def read_profile(self) -> Result:
        return await self._request("GET", "/profile")

    async def save_profile(self, content: str) -> Result:
        return await self._request("PUT", "/profile", json={"content": content})

    async def edit_profile(self, old_string: str, new_string: str, replace_all: bool = False) -> Result:
        return await self._request(
            "PATCH", "/profile",
            json={"old_string": old_string, "new_string": new_string, "replace_all": replace_all},
        )

    # -------------------------------------------------------- applications
    async def search_applications(self, params: Dict[str, Any]) -> Result:
        # Drop None-valued params so they don't become the literal string "None".
        clean = {k: v for k, v in params.items() if v is not None}
        return await self._request("GET", "/applications", params=clean)

    async def create_application(self, company: str, title: str, job_description: str = "") -> Result:
        return await self._request(
            "POST", "/applications",
            json={"company": company, "title": title, "job_description": job_description},
        )

    async def get_application(self, application_id: str) -> Result:
        return await self._request("GET", f"/applications/{application_id}")

    async def update_application(self, application_id: str, fields: Dict[str, Any]) -> Result:
        return await self._request("PATCH", f"/applications/{application_id}", json=fields)

    async def delete_application(self, application_id: str) -> Result:
        return await self._request("DELETE", f"/applications/{application_id}")

    # ------------------------------------------------------------- contacts
    async def add_contact(self, application_id: str, contact: Dict[str, Any]) -> Result:
        return await self._request("POST", f"/applications/{application_id}/contacts", json=contact)

    # ----------------------------------------------------------- preferences
    # Agent-authored durable coaching preferences (P7 #17). Kept physically
    # separate from the profile/projects (never in the grounding corpus).
    async def add_preference(self, content: str) -> Result:
        return await self._request("POST", "/preferences", json={"content": content})

    async def list_preferences(self) -> Result:
        return await self._request("GET", "/preferences")

    # -------------------------------------------------------------- resumes
    async def save_resume(self, application_id: str, content: str) -> Result:
        return await self._request("PUT", f"/applications/{application_id}/resume", json={"content": content})

    async def edit_resume(
        self, application_id: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> Result:
        return await self._request(
            "PATCH", f"/applications/{application_id}/resume",
            json={"old_string": old_string, "new_string": new_string, "replace_all": replace_all},
        )

    # ------------------------------------------------------------- artifacts
    # Rendered résumé documents (PDF/DOCX bytes) — P7 #16. The bytes ride JSON as
    # base64 on the way IN (save), and come back as a raw binary body on the way
    # OUT (the download proxy), so get_artifact_bytes bypasses the JSON _request.
    async def save_artifact(
        self, application_id: str, fmt: str, filename: str, content_b64: str,
        ats: Optional[Dict[str, Any]] = None,
    ) -> Result:
        payload: Dict[str, Any] = {"format": fmt, "filename": filename, "content_b64": content_b64}
        if ats:
            for k in ("ats_score", "ats_coverage", "ats_matched", "ats_missing"):
                if ats.get(k) is not None:
                    payload[k] = ats[k]
        return await self._request("POST", f"/applications/{application_id}/artifact", json=payload)

    async def get_artifact_bytes(
        self, application_id: str, artifact_id: Optional[str] = None
    ) -> Tuple[int, Optional[bytes], str, str]:
        """GET a rendered artifact's RAW bytes (for the api download proxy). Returns
        (status, content|None, media_type, filename). content is None on any non-200.
        A transport failure (dossier unreachable) returns status 0 — distinct from a
        real 404 (artifact absent) or a 5xx (dossier fault) so the caller can map the
        HTTP status correctly. Never raises on a transport error; raises only if used
        before start()."""
        if self._http is None:
            raise RuntimeError("DossierClient used before start().")
        params = {"artifact_id": artifact_id} if artifact_id else None
        try:
            resp = await self._http.get(f"/applications/{application_id}/artifact", params=params)
        except httpx.HTTPError as err:
            logger.warning("get_artifact_bytes transport error: %s: %s", type(err).__name__, err)
            return 0, None, "", ""
        if resp.status_code != 200:
            return resp.status_code, None, "", ""
        media = resp.headers.get("content-type", "application/octet-stream")
        cd = resp.headers.get("content-disposition", "")
        m = re.search(r'filename="([^"]+)"', cd)
        filename = m.group(1) if m else "resume"
        return 200, resp.content, media, filename

    # -------------------------------------------------------------- projects
    async def create_project(self, fields: Dict[str, Any]) -> Result:
        # Drop None so dossier's non-Optional defaults (source, summary) aren't
        # overridden with null.
        clean = {k: v for k, v in fields.items() if v is not None}
        return await self._request("POST", "/projects", json=clean)

    async def search_projects(self, params: Dict[str, Any]) -> Result:
        clean = {k: v for k, v in params.items() if v is not None}
        return await self._request("GET", "/projects", params=clean)

    async def get_project(self, project_id: str) -> Result:
        return await self._request("GET", f"/projects/{project_id}")

    async def update_project(self, project_id: str, fields: Dict[str, Any]) -> Result:
        return await self._request("PATCH", f"/projects/{project_id}", json=fields)

    async def delete_project(self, project_id: str) -> Result:
        return await self._request("DELETE", f"/projects/{project_id}")
