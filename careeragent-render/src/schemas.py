#!/usr/bin/env python3
# ============================================================================
# careeragent-render - request/response schemas
# ============================================================================

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class RenderRequest(BaseModel):
    """Body for POST /render — a résumé to render into a document.

    The résumé arrives as markdown text the caller already has (e.g. the coach's
    edited draft); this service does no fetching and no storage.

    - resume: the résumé as markdown text. Empty/whitespace is a 400 (there is
      nothing to render); see backend.api. A focused subset is supported —
      headings, bullets, **bold**/*italic*, paragraphs, `---` rules.
    - format: the target document format, "pdf" or "docx" (case-insensitive).
      Anything else is a 400.
    - title: optional document title / filename hint. When present it sets the
      document metadata title and is slugified into the suggested filename.
    """
    resume: str
    format: str
    title: Optional[str] = None


class RenderResponse(BaseModel):
    """POST /render success (200) — the rendered document bytes, base64-encoded.

    This service is stateless: it returns the bytes and stores nothing. The
    caller (careeragent-api) persists them (e.g. via careeragent-dossier).

    - content_b64: base64 of the raw file bytes (decode to recover the file).
    - format: the format actually rendered — "pdf" or "docx".
    - bytes: length of the DECODED content, in bytes (a convenience/sanity check
      for the caller; equals len(base64.b64decode(content_b64))).
    - filename: a suggested download filename, e.g. "resume.pdf".
    """
    content_b64: str
    format: str
    bytes: int
    filename: str
