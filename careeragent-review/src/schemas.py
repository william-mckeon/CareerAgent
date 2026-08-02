#!/usr/bin/env python3
# ============================================================================
# careeragent-review - request/response schemas
# ============================================================================

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class ReviewRequest(BaseModel):
    """Body for POST /review-batch.

    - repos: explicit "owner/repo" list to review. If omitted, the harness
      enumerates the authenticated user's repos via the GitHub MCP.
    - limit: cap the number of repos reviewed this call (defaults to MAX_REPOS).
    - focus: optional lens passed to each per-repo reviewer (e.g. "backend",
      "the ML work") to bias what it surfaces.
    - force: re-review even if the stored commit_sha matches (skip idempotency).
    """
    repos: Optional[List[str]] = None
    limit: Optional[int] = None
    focus: Optional[str] = None
    force: bool = False


class RepoOutcome(BaseModel):
    """One repo's result in the batch response."""
    repo: str                       # owner/repo
    status: str                     # reviewed | skipped | error
    detail: Optional[str] = None    # reason (skipped: 'unchanged'; error: message)
    project_id: Optional[str] = None
    commit_sha: Optional[str] = None


class ReviewBatchResponse(BaseModel):
    """POST /review-batch response — a per-repo outcome list + counts."""
    reviewed: int
    skipped: int
    errors: int
    outcomes: List[RepoOutcome]
