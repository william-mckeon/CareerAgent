#!/usr/bin/env python3
# ============================================================================
# careeragent-code - safety primitives (repo-name validation + path guard)
# ============================================================================
#
# This box clones third-party repos and reads files out of them, so the two
# ways an attacker-controlled input could bite are (1) a crafted `repo` string
# that escapes the cache root or injects a git flag, and (2) a `path` that
# escapes a repo's directory (../, an absolute path, or a symlink pointing out).
# Both are blocked here; every route validates through these before touching disk.
# ============================================================================

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# owner/repo — GitHub allows [A-Za-z0-9._-] in each segment. NO slashes beyond the
# single separator, no '..', no leading '-' (which could be read as a git flag).
_REPO_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*/[A-Za-z0-9._][A-Za-z0-9._-]*$")


class CodeProblem(Exception):
    """A typed failure whose ``status_code`` maps 1:1 onto the HTTP response.
    400 = bad input; 404 = repo not synced / file missing; 413 = too big;
    502 = git/rg failure; 504 = timeout."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def valid_repo(repo: str) -> bool:
    """True iff ``repo`` is a safe 'owner/repo' — the ONLY shape we clone. Rejects
    '..', absolute paths, extra slashes, and anything that could be read as a flag."""
    r = (repo or "").strip()
    if not _REPO_RE.match(r):
        return False
    return ".." not in r.split("/")  # belt-and-braces vs a '..' segment


def require_repo(repo: str) -> str:
    r = (repo or "").strip()
    if not valid_repo(r):
        raise CodeProblem(400, "repo must be a valid 'owner/repo' name")
    return r


def resolve_in_repo(repo_dir: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` INSIDE ``repo_dir`` and return the real path, or raise.

    Rejects absolute paths, '..' traversal, and any resolved target (incl. through
    a symlink) that lands outside repo_dir. repo_dir is resolved once; the candidate
    is resolved (following symlinks) and must stay within it."""
    rel = (rel_path or "").strip().lstrip("/\\")
    if not rel:
        raise CodeProblem(400, "a 'path' is required")
    if ".git" in Path(rel).parts:
        raise CodeProblem(400, "the .git directory is not readable")
    base = repo_dir.resolve()
    candidate = (base / rel).resolve()
    try:
        candidate.relative_to(base)          # raises if candidate escapes base
    except ValueError:
        raise CodeProblem(400, "path escapes the repository")
    return candidate
