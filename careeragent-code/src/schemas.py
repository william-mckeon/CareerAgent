#!/usr/bin/env python3
# ============================================================================
# careeragent-code - request/response schemas
# ============================================================================

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class SyncRequest(BaseModel):
    """Body for POST /sync — the 'owner/repo' to clone/refresh."""
    repo: str


class SyncResponse(BaseModel):
    repo: str
    head_sha: str
    files: int
    bytes: int
    cached: bool          # True if it already existed (a pull), False if freshly cloned


class GrepRequest(BaseModel):
    """Body for POST /grep — ripgrep a synced repo."""
    repo: str
    pattern: str
    glob: Optional[str] = None


class GrepMatch(BaseModel):
    path: str
    line: int
    text: str


class GrepResponse(BaseModel):
    repo: str
    matches: List[GrepMatch]
    truncated: bool


class FileResponse(BaseModel):
    repo: str
    path: str
    content: str
    bytes: int
    truncated: bool


class TreeEntry(BaseModel):
    path: str
    bytes: int


class TreeResponse(BaseModel):
    repo: str
    entries: List[TreeEntry]
    truncated: bool


class RepoInfo(BaseModel):
    repo: str
    head_sha: str
    last_used: int


class RefreshRequest(BaseModel):
    """Body for POST /refresh — an optional cap on how many repos to warm this
    sweep (still clamped to the server's max_refresh_repos)."""
    limit: Optional[int] = None


class RefreshResponse(BaseModel):
    discovered: int       # owner repos the GitHub API returned
    refreshed: int        # successfully cloned/pulled this sweep
    skipped: int          # discovered but not attempted (hit the count/byte bound)
    errors: int           # per-repo failures (validated/oversized/unreachable) — sweep continued
    repos: List[str]      # the repos actually warmed
    bytes: int            # total bytes warmed this sweep
