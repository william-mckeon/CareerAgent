"""
src/schemas.py

Request models for careeragent-dossier's tool endpoints. Each corresponds to one
agent tool (see specs/0001-dossier.md). Responses are returned as plain dicts by
the API layer, so only inputs are modelled here.
"""
from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


class EditRequest(BaseModel):
    """Exact-match, in-place edit — used by edit_profile and edit_resume.

    old_string must match exactly and (unless replace_all) uniquely; the store
    refuses ambiguous edits rather than risk silent corruption.
    """
    old_string: str
    new_string: str
    replace_all: bool = False


class SaveProfile(BaseModel):
    """Body for PUT /profile (save_profile) — set the master profile wholesale.

    Used to seed the profile (from an interview or an uploaded resume) or replace
    it outright; edit_profile handles precise in-place tweaks afterwards.
    """
    content: str


class CreateApplication(BaseModel):
    """Body for POST /applications (create_application)."""
    company: str
    title: str
    job_description: str = ""


class UpdateApplication(BaseModel):
    """Body for PATCH /applications/{id} (update_application).

    Every field optional — only the ones supplied are changed. Never touches
    final_resume (that has its own save/edit endpoints). All-None is a no-op.
    """
    status: Optional[str] = None
    last_contact: Optional[datetime] = None
    next_follow_up: Optional[date] = None
    applied_at: Optional[datetime] = None
    posting_url: Optional[str] = None
    location: Optional[str] = None
    salary_range: Optional[str] = None
    notes: Optional[str] = None
    company: Optional[str] = None
    title: Optional[str] = None
    job_description: Optional[str] = None


class CreateProject(BaseModel):
    """Body for POST /projects (create/upsert a project in the evidence library).

    If external_id (e.g. a GitHub 'owner/repo') is supplied and already exists,
    the row is UPDATED in place — so re-reviewing a repo refreshes it rather than
    duplicating. Manually-added projects leave external_id null.
    """
    name: str
    source: str = "manual"                       # github | manual | resume
    external_id: Optional[str] = None            # e.g. GitHub 'owner/repo'
    repo_url: Optional[str] = None
    summary: str = ""
    role: Optional[str] = None
    tech_stack: Optional[str] = None
    highlights: Optional[str] = None
    languages: Optional[str] = None
    stars: Optional[int] = None
    last_reviewed_at: Optional[datetime] = None
    commit_sha: Optional[str] = None             # reviewed HEAD sha (idempotency)


class UpdateProject(BaseModel):
    """Body for PATCH /projects/{id} — only supplied fields change; all-None is a no-op."""
    name: Optional[str] = None
    source: Optional[str] = None
    external_id: Optional[str] = None
    repo_url: Optional[str] = None
    summary: Optional[str] = None
    role: Optional[str] = None
    tech_stack: Optional[str] = None
    highlights: Optional[str] = None
    languages: Optional[str] = None
    stars: Optional[int] = None
    last_reviewed_at: Optional[datetime] = None
    commit_sha: Optional[str] = None


class AddContact(BaseModel):
    """Body for POST /applications/{id}/contacts (add_contact)."""
    name: str
    role: Optional[str] = None
    source: Optional[str] = None
    contact_info: Optional[str] = None
    notes: Optional[str] = None


class SaveResume(BaseModel):
    """Body for PUT /applications/{id}/resume (save_resume) — wholesale replace."""
    content: str


class SavePreference(BaseModel):
    """Body for POST /preferences (the agent's `remember` tool, P7 #17).

    A single user-STATED coaching preference to pin as a standing instruction.
    Deliberately NOT career evidence — kept out of the grounding corpus.
    """
    content: str


class SaveArtifact(BaseModel):
    """Body for POST /applications/{id}/artifact (P7 #16) — store a rendered
    résumé document (PDF/DOCX) careeragent-render produced.

    The bytes arrive base64-encoded so they can ride JSON on the api->dossier hop
    (the api never streams raw bytes through the tool result / SSE content stream).
    The ats_* fields are reserved for pairing the artifact with its coverage score.
    """
    format: str                                  # 'pdf' | 'docx'
    filename: str
    content_b64: str                             # base64 of the raw document bytes
    ats_score: Optional[int] = None
    ats_coverage: Optional[str] = None
    ats_matched: Optional[List[str]] = None
    ats_missing: Optional[List[str]] = None
