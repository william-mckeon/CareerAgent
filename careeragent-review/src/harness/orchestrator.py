#!/usr/bin/env python3
# ============================================================================
# careeragent-review - orchestrator (partition in code, fan out, reduce)
# ============================================================================
#
# The OpenCode spine: the HARNESS decides the decomposition (one child per
# repo), bounds it (cap the repo count, semaphore the concurrency), guarantees
# each child is isolated (its own /complete context), and only reduces the
# structured results. The model never sees more than one repo at a time.
#
# Unlike OpenCode (in-process, sequential), the children here are independent
# HTTP round-trips, so the fan-out is PARALLEL (asyncio.gather under a
# semaphore) — N repos reviewed in ~one repo's latency.
#
# Idempotency: before reviewing, best-effort compare the repo's current HEAD sha
# to the commit_sha dossier stored last time; skip if unchanged (unless force).
# ============================================================================

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from client.dossier import DossierClient
from client.infra import InfraClient
from client.mcp_client import MCPClient
from schemas import RepoOutcome, ReviewBatchResponse, ReviewRequest

from .subagent import review_one

logger = logging.getLogger("careeragent-review")

# Control calls (head sha, enumeration) must NOT be truncated at the model-facing
# 6000-char cap, or their JSON won't parse (idempotency then silently fails open).
_CONTROL_MAX_CHARS = 200_000


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


async def _head_sha(mcp: MCPClient, owner: str, repo: str) -> Optional[str]:
    """Best-effort current HEAD commit sha via list_commits(perPage=1). Returns
    None on any failure (idempotency then fails open → we review)."""
    ok, text = await mcp.call(
        mcp.prefix + "list_commits", {"owner": owner, "repo": repo, "perPage": 1},
        max_chars=_CONTROL_MAX_CHARS,
    )
    if not ok:
        return None
    data = _loads(text)
    commits = data.get("commits") if isinstance(data, dict) else data
    if isinstance(commits, list) and commits and isinstance(commits[0], dict):
        sha = commits[0].get("sha")
        return sha if isinstance(sha, str) else None
    return None


async def _enumerate_repos(mcp: MCPClient, limit: int) -> List[str]:
    """Best-effort: the authenticated user's repos as owner/repo strings. Empty
    list on failure (caller then reports 'pass repos explicitly')."""
    ok, me = await mcp.call(mcp.prefix + "get_me", {}, max_chars=_CONTROL_MAX_CHARS)
    login = None
    if ok:
        data = _loads(me)
        if isinstance(data, dict):
            login = data.get("login")
    if not login:
        return []
    ok, text = await mcp.call(
        mcp.prefix + "search_repositories",
        {"query": f"user:{login} sort:updated", "perPage": limit},
        max_chars=_CONTROL_MAX_CHARS,
    )
    if not ok:
        return []
    data = _loads(text)
    items = data.get("items") if isinstance(data, dict) else data
    out: List[str] = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                fn = it.get("full_name") or (
                    f"{login}/{it.get('name')}" if it.get("name") else None
                )
                if isinstance(fn, str):
                    out.append(fn)
    return out[:limit]


class Orchestrator:
    """Holds the shared clients + knobs; runs a review batch."""

    def __init__(
        self,
        *,
        infra: InfraClient,
        mcp: MCPClient,
        dossier: DossierClient,
        max_repos: int = 12,
        concurrency: int = 4,
        per_repo_max_steps: int = 12,
        review_model: str = "base",
        review_effort: str = "low",
    ) -> None:
        self._infra = infra
        self._mcp = mcp
        self._dossier = dossier
        self._max_repos = max_repos
        self._concurrency = concurrency
        self._per_repo_max_steps = per_repo_max_steps
        self._review_model = review_model
        self._review_effort = review_effort

    async def review_batch(self, request: ReviewRequest) -> ReviewBatchResponse:
        focus = request.focus
        # Clamp: treat missing/non-positive limit as "unset" → MAX_REPOS.
        n = request.limit
        cap = self._max_repos if (not n or n < 1) else min(n, self._max_repos)

        repos = request.repos or await _enumerate_repos(self._mcp, cap)
        # de-dup, keep order, drop blanks, cap
        seen, ordered = set(), []
        for r in repos:
            r = (r or "").strip()
            if r and "/" in r and r not in seen:
                seen.add(r)
                ordered.append(r)
        ordered = ordered[:cap]

        if not ordered:
            return ReviewBatchResponse(
                reviewed=0, skipped=0, errors=1,
                outcomes=[RepoOutcome(
                    repo="(none)", status="error",
                    detail="no repos to review — pass an explicit 'repos' list "
                           "(enumeration via the GitHub MCP returned nothing).",
                )],
            )

        sem = asyncio.Semaphore(self._concurrency)

        async def _one(repo: str) -> RepoOutcome:
            async with sem:
                return await self._review_and_store(repo, focus, request.force)

        # return_exceptions=True so a BaseException escaping a child (e.g. anyio
        # teardown) is captured as an error outcome — never propagated (which would
        # 500 the batch and orphan the still-running siblings).
        raw = await asyncio.gather(*[_one(r) for r in ordered], return_exceptions=True)
        outcomes = [
            res if isinstance(res, RepoOutcome)
            else RepoOutcome(repo=repo, status="error", detail=f"{type(res).__name__}: {res}")
            for repo, res in zip(ordered, raw)
        ]
        reviewed = sum(o.status == "reviewed" for o in outcomes)
        skipped = sum(o.status == "skipped" for o in outcomes)
        errors = sum(o.status == "error" for o in outcomes)
        return ReviewBatchResponse(
            reviewed=reviewed, skipped=skipped, errors=errors, outcomes=list(outcomes)
        )

    async def _review_and_store(self, repo: str, focus: Optional[str], force: bool) -> RepoOutcome:
        owner, _, name = repo.partition("/")
        # If the GitHub MCP failed to connect, there are no read tools — don't
        # waste a model context per repo; short-circuit to an error.
        if not getattr(self._mcp, "started", False):
            return RepoOutcome(repo=repo, status="error",
                               detail="GitHub tools unavailable (careeragent-github-mcp not connected).")
        try:
            head = await _head_sha(self._mcp, owner, name)  # may be None (fail-open)
            if not force and head:
                existing = await self._dossier.get_by_external_id(repo)
                if existing and existing.get("commit_sha") == head:
                    return RepoOutcome(repo=repo, status="skipped", detail="unchanged", commit_sha=head)

            fields = await review_one(
                repo, focus=focus, infra=self._infra, mcp=self._mcp,
                max_steps=self._per_repo_max_steps, model=self._review_model,
                reasoning_effort=self._review_effort,
            )
            if not fields:
                return RepoOutcome(repo=repo, status="error", detail="no structured review produced")

            write = {
                **fields,
                "source": "github",
                "external_id": repo,
                "commit_sha": head,
                "last_reviewed_at": datetime.now(timezone.utc).isoformat(),
            }
            write.setdefault("name", name)
            if not write.get("repo_url"):
                write["repo_url"] = f"https://github.com/{repo}"

            status, body = await self._dossier.save_project(write)
            if status not in (200, 201):
                return RepoOutcome(repo=repo, status="error", detail=f"dossier {status}: {body}")
            pid = body.get("id") if isinstance(body, dict) else None
            return RepoOutcome(repo=repo, status="reviewed", project_id=pid, commit_sha=head)
        except Exception as err:
            logger.warning("review_and_store(%s) failed: %s: %s", repo, type(err).__name__, err)
            return RepoOutcome(repo=repo, status="error", detail=f"{type(err).__name__}: {err}")
