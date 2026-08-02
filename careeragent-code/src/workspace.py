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
        max_refresh_repos: int = 20,
        refresh_budget_bytes: Optional[int] = None,
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
        # Nightly-refresh bounds (Slice E). The sweep stops at whichever comes first —
        # a repo count or a byte budget. The budget defaults to (cache cap − one repo
        # ceiling) so that even the check-before-warm overshoot (one last repo up to
        # max_repo_bytes) lands within the cap; it is clamped to the cap. Belt-and-
        # suspenders: refresh() also protects warmed repos from end-of-sweep eviction,
        # so no warmed repo is evicted even if this budget is misconfigured.
        self.max_refresh_repos = max_refresh_repos
        _budget = refresh_budget_bytes if refresh_budget_bytes else max(0, max_cache_bytes - max_repo_bytes)
        self.refresh_budget_bytes = min(_budget, max_cache_bytes)

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
    def _sync_one(self, repo: str) -> tuple:
        """Clone-or-pull ONE repo + measure + enforce the per-repo cap + mark used.
        Does NOT enforce the GLOBAL cache cap — the caller decides when: sync() does
        it once per call; refresh() does it once at the end of the whole sweep (so a
        bounded warm never evicts a repo it warmed earlier in the same pass)."""
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
        return {"repo": repo, "head_sha": head, "files": files, "bytes": byts,
                "cached": existed}, d

    def sync(self, repo: str) -> Dict[str, Any]:
        result, d = self._sync_one(repo)
        self._enforce_cache_cap(exclude=d)   # never evict the repo we just synced
        return result

    # ------------------------------------------------------------- refresh (Slice E)
    def refresh(self, max_repos: Optional[int] = None) -> Dict[str, Any]:
        """Nightly warm: DISCOVER the user's owner repos (newest-pushed first) and
        clone-or-pull each, BOUNDED by a repo count and a byte budget so the sweep
        can never exceed the cache and thus never evicts what it just warmed.

        Fail-soft PER REPO: one bad/oversized/unreachable repo is counted in
        ``errors`` and the sweep continues. A DISCOVERY failure (no token / GitHub
        unreachable) raises CodeProblem — a TOTAL failure the caller (the nightly
        job) treats as a retry. The on-demand sync() path is untouched; this only
        pre-warms the cache."""
        discovered = gitops.list_owner_repos(self._token, timeout=self.rg_timeout)
        cap = (self.max_refresh_repos if max_repos is None
               else min(int(max_repos), self.max_refresh_repos))
        refreshed: List[str] = []
        warmed_dirs: List[Path] = []
        errors = 0
        warmed_bytes = 0
        for repo in discovered:
            if len(refreshed) >= cap or warmed_bytes >= self.refresh_budget_bytes:
                break                                  # bounded — leave the rest cold
            try:
                repo_v = require_repo(repo)            # untrusted API payload → validate
                result, d = self._sync_one(repo_v)
            except CodeProblem as exc:
                errors += 1
                logger.info("refresh: skipped %s (%s)", repo, getattr(exc, "detail", exc))
                continue
            refreshed.append(result["repo"])
            warmed_dirs.append(d)
            # Budget on FULL on-disk bytes (incl .git) — the SAME basis the cache cap
            # measures — so the stop condition can't undercount and overrun the cap.
            warmed_bytes += self._dir_bytes(d)
        # Tidy the cache, but NEVER evict a repo we warmed this pass — so the sweep can
        # never clone-then-evict its own work (invariant #3), only shed older repos.
        self._enforce_cache_cap(exclude=warmed_dirs)
        skipped = max(0, len(discovered) - len(refreshed) - errors)
        return {"discovered": len(discovered), "refreshed": len(refreshed),
                "skipped": skipped, "errors": errors, "repos": refreshed,
                "bytes": warmed_bytes}

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

    def _enforce_cache_cap(self, exclude: Any = None) -> None:
        """Evict least-recently-used repos until total cache is under the cap. Repos
        in ``exclude`` (a single dir or an iterable of them) are never evicted — so a
        sync never deletes its own just-synced repo, and a refresh sweep never evicts a
        repo it warmed THIS pass (protecting invariant #3 regardless of byte-accounting)."""
        if exclude is None:
            keep = set()
        elif isinstance(exclude, (list, tuple, set)):
            keep = {p.resolve() for p in exclude}
        else:
            keep = {exclude.resolve()}
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
            if d.resolve() in keep:
                continue                              # never evict a protected repo
            sz = self._dir_bytes(d)
            logger.info("evicting %s/%s (%d bytes) — cache over cap", d.parent.name, d.name, sz)
            shutil.rmtree(d, ignore_errors=True)
            total -= sz
