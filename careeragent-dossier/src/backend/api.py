"""
src/backend/api.py

careeragent-dossier — the career data system-of-record for CareerAgent.

Owns the master profile and the application tracker (applications, contacts,
tailored resumes with version history). Exposes each of the agent's data tools as
one HTTP endpoint. No vectors — lookup is structured filters + full-text search +
trigram fuzzy. Holds NO agent logic (no loop, no permission engine, no persona);
those live in careeragent-api. See specs/0001-dossier.md.

Endpoints (all X-API-Key except /health):
  GET    /profile                          read_profile
  PUT    /profile                          save_profile        (wholesale replace)
  PATCH  /profile                          edit_profile        (exact-match)
  GET    /applications                     search_applications (filters + FTS + trigram)
  POST   /applications                     create_application
  GET    /applications/{id}                get_application
  PATCH  /applications/{id}                update_application  (structured fields)
  DELETE /applications/{id}                delete_application
  POST   /applications/{id}/contacts       add_contact
  PUT    /applications/{id}/resume         save_resume         (wholesale replace)
  PATCH  /applications/{id}/resume         edit_resume         (exact-match)
  GET    /projects                         search_projects     (filters + FTS + trigram)
  POST   /projects                         create_project      (upsert by external_id)
  GET    /projects/{id}                    get_project
  PATCH  /projects/{id}                    update_project
  DELETE /projects/{id}                    delete_project
  GET    /health                           no auth
"""
import base64
import logging
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse, Response

from schemas import (
    AddContact,
    CreateApplication,
    CreateProject,
    EditRequest,
    SaveArtifact,
    SavePreference,
    SaveProfile,
    SaveResume,
    UpdateApplication,
    UpdateProject,
)
from security import verify_api_key
from store import ConflictError, EditError, Store

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("careeragent-dossier")

ENABLE_DOCS = os.environ.get("DOSSIER_ENABLE_DOCS", "").strip().lower() == "true"

store: Optional[Store] = None


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store
    store = Store()
    db_ok = await store.ping()
    logger.info("=== careeragent-dossier starting ===")
    logger.info("Port        : %s", os.environ.get("DOSSIER_PORT", "8006"))
    logger.info("DB schema   : %s", store._schema)
    logger.info("Database    : %s", "ok" if db_ok else "UNREACHABLE")
    logger.info("API docs    : %s", "enabled" if ENABLE_DOCS else "disabled")
    logger.info("=== careeragent-dossier ready on :%s ===", os.environ.get("DOSSIER_PORT", "8006"))
    yield
    await store.stop()
    logger.info("=== careeragent-dossier shutting down ===")


app = FastAPI(
    title="careeragent-dossier",
    description="Career data system-of-record for CareerAgent (profile + application tracker).",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def _edit_http(exc: EditError) -> HTTPException:
    """Map an exact-match EditError to the house HTTP shape + a teaching message."""
    if exc.kind == "not_found":
        return HTTPException(
            status_code=422,
            detail="old_string not found — re-read the current text and copy an exact substring.",
        )
    return HTTPException(
        status_code=409,
        detail=(
            f"old_string is not unique ({exc.count} matches) — add surrounding "
            "context to make it unique, or set replace_all=true."
        ),
    )


def _require_uuid(application_id: str) -> None:
    if not _valid_uuid(application_id):
        raise HTTPException(status_code=400, detail="application_id must be a valid UUID")


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------
@app.get("/profile")
async def read_profile(api_key: str = Security(verify_api_key)):
    return await store.get_profile()


@app.put("/profile")
async def save_profile(body: SaveProfile, api_key: str = Security(verify_api_key)):
    return await store.save_profile(body.content)


@app.patch("/profile")
async def edit_profile(body: EditRequest, api_key: str = Security(verify_api_key)):
    try:
        return await store.edit_profile(body.old_string, body.new_string, body.replace_all)
    except EditError as exc:
        raise _edit_http(exc)


# ---------------------------------------------------------------------------
# applications
# ---------------------------------------------------------------------------
@app.get("/applications")
async def search_applications(
    api_key: str = Security(verify_api_key),
    status: Optional[str] = None,
    company: Optional[str] = None,
    q: Optional[str] = None,
    applied_after: Optional[datetime] = None,
    applied_before: Optional[datetime] = None,
    stale: Optional[bool] = None,
    follow_up_due: Optional[bool] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await store.search_applications(
        status=status,
        company=company,
        q=q,
        applied_after=applied_after,
        applied_before=applied_before,
        stale=stale,
        follow_up_due=follow_up_due,
        limit=limit,
        offset=offset,
    )


@app.post("/applications", status_code=201)
async def create_application(body: CreateApplication, api_key: str = Security(verify_api_key)):
    if not body.company.strip() or not body.title.strip():
        raise HTTPException(status_code=400, detail="company and title are required")
    application_id = await store.create_application(body.company, body.title, body.job_description)
    return {"id": application_id}


@app.get("/applications/{application_id}")
async def get_application(application_id: str, api_key: str = Security(verify_api_key)):
    _require_uuid(application_id)
    app_row = await store.get_application(application_id)
    if app_row is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return app_row


@app.patch("/applications/{application_id}")
async def update_application(
    application_id: str, body: UpdateApplication, api_key: str = Security(verify_api_key)
):
    _require_uuid(application_id)
    updated = await store.update_application(application_id, body.model_dump())
    if updated is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return updated


@app.delete("/applications/{application_id}")
async def delete_application(application_id: str, api_key: str = Security(verify_api_key)):
    _require_uuid(application_id)
    if not await store.delete_application(application_id):
        raise HTTPException(status_code=404, detail="Application not found")
    return {"deleted": application_id}


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------
@app.post("/applications/{application_id}/contacts", status_code=201)
async def add_contact(
    application_id: str, body: AddContact, api_key: str = Security(verify_api_key)
):
    _require_uuid(application_id)
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    contact_id = await store.add_contact(application_id, body.model_dump())
    if contact_id is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return {"contact_id": contact_id}


# ---------------------------------------------------------------------------
# preferences — agent-authored durable coaching preferences (P7 #17)
# ---------------------------------------------------------------------------
@app.post("/preferences", status_code=201)
async def add_preference(body: SavePreference, api_key: str = Security(verify_api_key)):
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    preference_id = await store.add_preference(body.content)
    return {"id": preference_id}


@app.get("/preferences")
async def list_preferences(api_key: str = Security(verify_api_key)):
    return await store.list_preferences()


@app.delete("/preferences/{preference_id}")
async def delete_preference(preference_id: str, api_key: str = Security(verify_api_key)):
    if not _valid_uuid(preference_id):
        raise HTTPException(status_code=400, detail="preference_id must be a valid UUID")
    if not await store.delete_preference(preference_id):
        raise HTTPException(status_code=404, detail="Preference not found")
    return {"deleted": preference_id}


# ---------------------------------------------------------------------------
# resume (per application)
# ---------------------------------------------------------------------------
@app.put("/applications/{application_id}/resume")
async def save_resume(
    application_id: str, body: SaveResume, api_key: str = Security(verify_api_key)
):
    _require_uuid(application_id)
    version = await store.save_resume(application_id, body.content)
    if version is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return {"version": version}


@app.patch("/applications/{application_id}/resume")
async def edit_resume(
    application_id: str, body: EditRequest, api_key: str = Security(verify_api_key)
):
    _require_uuid(application_id)
    try:
        result = await store.edit_resume(
            application_id, body.old_string, body.new_string, body.replace_all
        )
    except EditError as exc:
        raise _edit_http(exc)
    if result is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return result


# ---------------------------------------------------------------------------
# rendered artifacts (per application) — P7 #16
# ---------------------------------------------------------------------------
# Decoded-size cap. careeragent-render caps its INPUT résumé at ~200 KB; a real
# rendered PDF/DOCX is well under a few hundred KB. 5 MB is generous headroom that
# still stops a malformed/hostile body from bloating the DB.
_MAX_ARTIFACT_BYTES = 5_000_000
_ARTIFACT_MEDIA = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _safe_filename(name: str, fmt: str) -> str:
    """A header-safe download filename. Strips anything that could break (or inject
    into) the Content-Disposition header, and guarantees the right extension."""
    base = re.sub(r'[\r\n"\\/]+', "", (name or "").strip()) or "resume"
    base = base[:120]
    if not base.lower().endswith(f".{fmt}"):
        base = f"{base}.{fmt}"
    return base


@app.post("/applications/{application_id}/artifact", status_code=201)
async def save_artifact(
    application_id: str, body: SaveArtifact, api_key: str = Security(verify_api_key)
):
    _require_uuid(application_id)
    fmt = (body.format or "").strip().lower()
    if fmt not in _ARTIFACT_MEDIA:
        raise HTTPException(status_code=400, detail="format must be 'pdf' or 'docx'")
    try:
        content = base64.b64decode(body.content_b64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="content_b64 is not valid base64")
    if not content:
        raise HTTPException(status_code=400, detail="artifact content is empty")
    if len(content) > _MAX_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="artifact too large to store")
    ats = {
        "score": body.ats_score,
        "coverage": body.ats_coverage,
        "matched": body.ats_matched,
        "missing": body.ats_missing,
    }
    result = await store.save_artifact(
        application_id, fmt, _safe_filename(body.filename, fmt), content, ats
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return result  # {id, version, byte_size}


@app.get("/applications/{application_id}/artifact")
async def get_artifact(
    application_id: str,
    artifact_id: Optional[str] = Query(None, description="Specific artifact id; omit for the latest."),
    api_key: str = Security(verify_api_key),
):
    _require_uuid(application_id)
    # A malformed artifact_id would reach the uuid-typed `id` column and raise an
    # asyncpg cast error (-> 500). Treat an invalid id as simply not-found (404).
    if artifact_id is not None and not _valid_uuid(artifact_id):
        raise HTTPException(status_code=404, detail="No rendered artifact for this application")
    art = await store.get_artifact(application_id, artifact_id)
    if art is None:
        raise HTTPException(status_code=404, detail="No rendered artifact for this application")
    media = _ARTIFACT_MEDIA.get(art["format"], "application/octet-stream")
    filename = _safe_filename(art["filename"], art["format"])
    # Full bytes are already in memory — a plain Response streams them out with the
    # right type + a download disposition. (No StreamingResponse needed.)
    return Response(
        content=art["content"],
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Artifact-Id": art["id"],
            "X-Artifact-Version": str(art["version"]),
        },
    )


# ---------------------------------------------------------------------------
# projects (the evidence library)
# ---------------------------------------------------------------------------
@app.get("/projects")
async def search_projects(
    api_key: str = Security(verify_api_key),
    q: Optional[str] = None,
    source: Optional[str] = None,
    name: Optional[str] = None,
    external_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await store.search_projects(
        q=q, source=source, name=name, external_id=external_id, limit=limit, offset=offset
    )


@app.post("/projects", status_code=201)
async def create_project(body: CreateProject, api_key: str = Security(verify_api_key)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    # exclude_unset so an upsert refresh only overwrites the fields the caller
    # actually sent — otherwise CreateProject's defaults (source="manual",
    # summary="") would silently blank a populated row on re-review.
    try:
        return await store.create_project(body.model_dump(exclude_unset=True))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/projects/{project_id}")
async def get_project(project_id: str, api_key: str = Security(verify_api_key)):
    _require_uuid(project_id)
    row = await store.get_project(project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


@app.patch("/projects/{project_id}")
async def update_project(
    project_id: str, body: UpdateProject, api_key: str = Security(verify_api_key)
):
    _require_uuid(project_id)
    try:
        updated = await store.update_project(project_id, body.model_dump())
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if updated is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return updated


@app.delete("/projects/{project_id}")
async def delete_project(project_id: str, api_key: str = Security(verify_api_key)):
    _require_uuid(project_id)
    if not await store.delete_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"deleted": project_id}


# ---------------------------------------------------------------------------
# health (no auth)
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    db_ok = await store.ping() if store else False
    return {
        "status": "ok" if db_ok else "degraded",
        "dossier": "ok",
        "database": "ok" if db_ok else "unreachable",
    }
