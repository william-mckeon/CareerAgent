#!/usr/bin/env python3
# ============================================================================
# careeragent-code - the cache manager (clone-on-demand, read-only access)
# ============================================================================
#
# Ties gitops + search + safety together over a cache-root volume. A repo is
# cloned on first `sync`, then read via `read_file` / `tree` / `grep`. Bounds:
# per-file size cap, per-repo file-count cap, a global LRU cap on total cache
# size, and per-op timeouts. The GitHub PAT lives here and is passed to gitops;
# it never leaves this box.
# ============================================================================

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import gitops
import search
from safety import CodeProblem, require_repo, resolve_in_repo

logger = logging.getLogger("careeragent-code")

# Dirs never surfaced in a tree/read (VCS internals + noise).
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              ".pytest_cache", "dist", "build", ".next", "target"}
# Our own per-repo LRU marker — never surface it in a tree.
_MARKER = ".careeragent_used"


class Workspace:
    def __init__(
        self,
        cache_root: str,
        token: Optional[str],
        *,
        git_timeout: float = 120.0,
        rg_timeout: float = 20.0,
        max_file_bytes: int = 400_000,
        max_tree_entries: int = 4000,
        max_grep_matches: int = 200,
        max_cache_bytes: int = 2_000_000_000,
        max_repo_bytes: int = 500_000_000,
    ) -> None:
        self.root = Path(cache_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._token = token
        self.git_timeout = git_timeout
        self.rg_timeout = rg_timeout
        self.max_file_bytes = max_file_bytes
        self.max_tree_entries = max_tree_entries
        self.max_grep_matches = max_grep_matches
        self.max_cache_bytes = max_cache_bytes
        self.max_repo_bytes = max_repo_bytes

    # ---------------------------------------------------------------- paths
    def _repo_dir(self, repo: str) -> Path:
        owner, name = repo.split("/", 1)
        return self.root / owner / name

    def _require_synced(self, repo: str) -> Path:
        repo = require_repo(repo)          # validate BEFORE splitting/building a path
        d = self._repo_dir(repo)
        if not (d / ".git").exists():
            raise CodeProblem(404, f"'{repo}' is not synced — call /sync first")
        return d

    # ---------------------------------------------------------------- sync
    def sync(self, repo: str) -> Dict[str, Any]:
        repo = require_repo(repo)
        d = self._repo_dir(repo)
        existed = (d / ".git").exists()
        head = gitops.clone_or_pull(repo, d, self._token, self.git_timeout)
        files, byts = self._measure(d)
        # A single repo bigger than the per-repo ceiling is refused (and removed) —
        # so one giant repo can't dominate the cache or later blow a read.
        if byts > self.max_repo_bytes:
            shutil.rmtree(d, ignore_errors=True)
            raise CodeProblem(413, f"'{repo}' is too large to review ({byts} bytes; "
                                   f"cap {self.max_repo_bytes})")
        self._mark_used(d)
        self._enforce_cache_cap(exclude=d)   # never evict the repo we just synced
        return {"repo": repo, "head_sha": head, "files": files, "bytes": byts,
                "cached": existed}

    # ---------------------------------------------------------------- read
    def read_file(self, repo: str, path: str) -> Dict[str, Any]:
        d = self._require_synced(repo)
        target = resolve_in_repo(d, path)
        if not target.is_file():
            raise CodeProblem(404, "file not found")
        # BOUNDED read: never materialize more than the cap + 1 byte (a cloned repo
        # is attacker-controlled data; a multi-GB file must not be pulled into RAM).
        size = target.stat().st_size
        with open(target, "rb") as fh:
            raw = fh.read(self.max_file_bytes + 1)
        truncated = len(raw) > self.max_file_bytes
        text = raw[: self.max_file_bytes].decode("utf-8", errors="replace")
        self._mark_used(d)
        return {"repo": repo, "path": path.strip().lstrip("/\\"),
                "content": text, "bytes": size, "truncated": truncated}

    def tree(self, repo: str) -> Dict[str, Any]:
        d = self._require_synced(repo)
        entries: List[Dict[str, Any]] = []
        truncated = False
        base = d.resolve()
        for cur, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if x not in _SKIP_DIRS]
            for fn in files:
                if fn == _MARKER:
                    continue
                if len(entries) >= self.max_tree_entries:
                    truncated = True
                    break
                p = Path(cur) / fn
                try:
                    rel = str(p.resolve().relative_to(base)).replace("\\", "/")
                    entries.append({"path": rel, "bytes": p.stat().st_size})
                except (ValueError, OSError):
                    continue
            if truncated:
                break
        entries.sort(key=lambda e: e["path"])
        self._mark_used(d)
        return {"repo": repo, "entries": entries, "truncated": truncated}

    def grep(self, repo: str, pattern: str, glob: Optional[str]) -> Dict[str, Any]:
        d = self._require_synced(repo)
        out = search.grep(d, pattern, glob, self.max_grep_matches, self.rg_timeout)
        self._mark_used(d)
        return {"repo": repo, **out}

    def list_repos(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for owner_dir in sorted(self.root.iterdir()) if self.root.exists() else []:
            if not owner_dir.is_dir():
                continue
            for name_dir in sorted(owner_dir.iterdir()):
                if (name_dir / ".git").exists():
                    repo = f"{owner_dir.name}/{name_dir.name}"
                    try:
                        head = gitops.head_sha(name_dir, timeout=self.rg_timeout)
                    except CodeProblem:
                        head = ""
                    out.append({"repo": repo, "head_sha": head,
                                "last_used": self._used_at(name_dir)})
        return out

    # ---------------------------------------------------------------- caps / LRU
    def _measure(self, d: Path) -> tuple:
        files = byts = 0
        for cur, dirs, fs in os.walk(d):
            if ".git" in Path(cur).parts:
                continue
            dirs[:] = [x for x in dirs if x != ".git"]
            for fn in fs:
                try:
                    byts += (Path(cur) / fn).stat().st_size
                    files += 1
                except OSError:
                    continue
        return files, byts

    def _dir_bytes(self, d: Path) -> int:
        total = 0
        for cur, _dirs, fs in os.walk(d):
            for fn in fs:
                try:
                    total += (Path(cur) / fn).stat().st_size
                except OSError:
                    continue
        return total

    def _mark_used(self, d: Path) -> None:
        try:
            (d / _MARKER).write_text(str(int(time.time())))
        except OSError:
            pass

    def _used_at(self, d: Path) -> int:
        try:
            return int((d / _MARKER).read_text().strip())
        except (OSError, ValueError):
            try:
                return int(d.stat().st_mtime)
            except OSError:
                return 0

    def _enforce_cache_cap(self, exclude: Optional[Path] = None) -> None:
        """Evict least-recently-used repos until total cache is under the cap. The
        just-synced repo (``exclude``) is never evicted — otherwise a sync that
        pushes the cache over the cap could immediately delete its own result."""
        keep = exclude.resolve() if exclude else None
        repos = []
        for owner_dir in self.root.iterdir() if self.root.exists() else []:
            if not owner_dir.is_dir():
                continue
            for name_dir in owner_dir.iterdir():
                if (name_dir / ".git").exists():
                    repos.append(name_dir)
        total = sum(self._dir_bytes(r) for r in repos)
        if total <= self.max_cache_bytes:
            return
        for d in sorted(repos, key=self._used_at):  # oldest first
            if total <= self.max_cache_bytes:
                break
            if keep is not None and d.resolve() == keep:
                continue                              # never evict the current repo
            sz = self._dir_bytes(d)
            logger.info("evicting %s/%s (%d bytes) — cache over cap", d.parent.name, d.name, sz)
            shutil.rmtree(d, ignore_errors=True)
            total -= sz
