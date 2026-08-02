#!/usr/bin/env python3
# ============================================================================
# careeragent-code - git operations (clone --depth 1 / pull, hardened)
# ============================================================================
#
# The ONLY things this box runs are `git` and `rg`, always with a fixed argv
# (never a shell). We clone READ-ONLY and never execute anything that comes FROM
# a cloned repo:
#   -c core.hooksPath=/dev/null   → no git hooks run (clone/checkout/pull)
#   -c core.symlinks=false        → symlinks are written as PLAIN files, so a
#                                    malicious repo can't plant a symlink that
#                                    escapes the cache dir (defence-in-depth with
#                                    safety.resolve_in_repo)
#   --depth 1 --single-branch --no-tags  → shallow; bounds history/size
#   GIT_TERMINAL_PROMPT=0, GIT_CONFIG_NOSYSTEM=1, no credential helper → git can
#                                    never block on a prompt or read host config.
# The PAT (private repos) rides the clone URL and is NEVER logged or returned.
# ============================================================================

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

from safety import CodeProblem

logger = logging.getLogger("careeragent-code")

_GITHUB_API = "https://api.github.com"

# git invoked with these -c overrides on EVERY call — the hardening above.
_GIT_HARDENING = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.symlinks=false",
    "-c", "protocol.version=2",
]


def _git_env(token: Optional[str] = None) -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"      # never prompt for credentials
    env["GIT_CONFIG_NOSYSTEM"] = "1"      # ignore /etc/gitconfig
    env["GCM_INTERACTIVE"] = "never"
    env["HOME"] = "/tmp"                   # no host ~/.gitconfig / credentials
    # Also drop any auth config injected by a prior call in a copied environ.
    for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        env.pop(k, None)
    if token:
        # Inject the PAT as an HTTP Authorization header via ENV-based config —
        # NOT the URL and NOT argv — so the token is never persisted to
        # .git/config on the cache volume nor visible in the process list. GitHub
        # accepts basic auth 'x-access-token:<PAT>'. (ADR-011: the PAT never
        # leaves this box, at rest or in transit.)
        b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {b64}"
    return env


def _run_git(args: list, timeout: float, cwd: Optional[Path] = None,
             token: Optional[str] = None) -> str:
    """Run one `git` command (fixed argv, no shell). ``token`` (clone/fetch only)
    is injected as an env-based auth header. Returns stdout; raises CodeProblem on
    non-zero/timeout. Never logs a URL/token."""
    try:
        proc = subprocess.run(
            ["git", *_GIT_HARDENING, *args],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd) if cwd else None, env=_git_env(token),
        )
    except subprocess.TimeoutExpired:
        raise CodeProblem(504, "git operation timed out")
    except FileNotFoundError:
        raise CodeProblem(502, "git is not available in this environment")
    if proc.returncode != 0:
        err = _scrub(proc.stderr.strip()) or f"git exited {proc.returncode}"
        raise CodeProblem(502, f"git failed: {err[:300]}")
    return proc.stdout


def _scrub(text: str) -> str:
    """Belt-and-braces: strip any 'user:token@' from a string before it could
    reach a log line or an HTTP response (the URL is tokenless now, but a
    misconfigured remote could still carry one)."""
    return re.sub(r"https://[^@/\s]+@", "https://***@", text or "")


def _clone_url(repo: str) -> str:
    """The TOKENLESS remote URL. Auth (private repos) rides an env-injected header,
    so the URL git persists to .git/config never contains the secret."""
    return f"https://github.com/{repo}.git"


def head_sha(repo_dir: Path, timeout: float = 20.0) -> str:
    out = _run_git(["rev-parse", "HEAD"], timeout=timeout, cwd=repo_dir)
    return out.strip()


def list_owner_repos(token: Optional[str], *, per_page: int = 100,
                     max_pages: int = 5, timeout: float = 20.0) -> List[str]:
    """Discover the authenticated user's OWNER repos via the GitHub REST API,
    newest-pushed first, as ['owner/name', ...].

    This is the ONE non-git use of the read-only PAT this box holds (ADR-011):
    `git` can clone/pull a named repo but cannot LIST a user's repos, so a nightly
    pre-sync needs this. The token rides an Authorization header (never a URL, never
    logged, never returned). Raises CodeProblem on a hard failure (no token /
    network / HTTP / malformed) so the caller can treat it as a total failure; the
    error text carries only a status code, never the token."""
    if not token:
        raise CodeProblem(400, "no GitHub token configured — cannot discover repos")
    full_names: List[str] = []
    for page in range(1, max_pages + 1):
        url = (f"{_GITHUB_API}/user/repos?affiliation=owner&sort=pushed"
               f"&per_page={int(per_page)}&page={page}")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "careeragent-code",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as err:
            raise CodeProblem(502, f"GitHub repo listing failed (HTTP {err.code})")
        except (urllib.error.URLError, OSError) as err:
            raise CodeProblem(502, f"GitHub repo listing failed: {type(err).__name__}")
        try:
            rows = json.loads(body)
        except Exception:
            raise CodeProblem(502, "GitHub repo listing returned malformed JSON")
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            if isinstance(r, dict):
                fn = r.get("full_name")
                if isinstance(fn, str) and "/" in fn:
                    full_names.append(fn)
        if len(rows) < per_page:
            break   # short page → last page
    return full_names


def clone_or_pull(repo: str, repo_dir: Path, token: Optional[str],
                  timeout: float) -> str:
    """Ensure ``repo_dir`` holds a fresh shallow checkout of ``repo``. Clones if
    absent, otherwise fetch+reset to the remote head. Returns the head sha. The
    PAT is passed only to the network ops (clone/fetch) via env, never persisted."""
    url = _clone_url(repo)
    if (repo_dir / ".git").exists():
        logger.info("pulling %s", repo)
        _run_git(["fetch", "--depth", "1", "--no-tags", url],
                 timeout=timeout, cwd=repo_dir, token=token)
        _run_git(["reset", "--hard", "FETCH_HEAD"], timeout=timeout, cwd=repo_dir)  # local, no token
    else:
        logger.info("cloning %s (shallow)", repo)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["clone", "--depth", "1", "--single-branch", "--no-tags", url, str(repo_dir)],
            timeout=timeout, token=token,
        )
    return head_sha(repo_dir, timeout=timeout)
