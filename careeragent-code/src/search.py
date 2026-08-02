#!/usr/bin/env python3
# ============================================================================
# careeragent-code - code search (ripgrep, bounded)
# ============================================================================
#
# ripgrep over ONE synced repo. The pattern is passed via `-e <pat>` so it can
# never be read as a flag; results are capped (match count + total bytes) so one
# broad query can't blow the caller's context, and a timeout bounds a pathological
# repo. Fixed argv, no shell.
# ============================================================================

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from safety import CodeProblem


def grep(repo_dir: Path, pattern: str, glob: Optional[str],
         max_matches: int, timeout: float) -> Dict[str, Any]:
    """ripgrep ``pattern`` under ``repo_dir``. Returns {matches:[{path,line,text}],
    truncated}. ``path`` is relative to the repo. Never raises on 'no matches'
    (rg exit 1) — only on a real rg failure/timeout."""
    pat = (pattern or "").strip()
    if not pat:
        raise CodeProblem(400, "a non-empty 'pattern' is required")
    argv = [
        "rg", "--line-number", "--no-heading", "--color", "never",
        "--hidden",                       # include dotfiles (.github/, configs)
        "--max-count", "50",              # per-file match cap
        "--max-filesize", "2M",           # skip huge/binary files
        "--max-columns", "500",           # bound a matched line's length (no 2 MB line)
        "--max-columns-preview",          # still show a preview of an over-long line
        "-e", pat,
    ]
    if glob:
        argv += ["--glob", glob]
    # Excludes LAST so they always win (rg gives precedence to later globs):
    # --hidden would otherwise descend into .git; and never surface our LRU marker.
    argv += ["--glob", "!.git", "--glob", "!.careeragent_used"]
    argv += ["--", "."]                   # search the repo dir (as cwd) → RELATIVE paths
    try:
        # cwd=repo_dir so rg emits repo-relative paths ("src/a.py"), never absolute
        # ones (a Windows drive letter's ':' would break the path:line:text split).
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              cwd=str(repo_dir))
    except subprocess.TimeoutExpired:
        raise CodeProblem(504, "search timed out")
    except FileNotFoundError:
        raise CodeProblem(502, "ripgrep is not available in this environment")
    # rg exit codes: 0 = matches, 1 = no matches, 2 = error.
    if proc.returncode not in (0, 1):
        raise CodeProblem(502, f"search failed: {(proc.stderr or '').strip()[:200]}")

    matches: List[Dict[str, Any]] = []
    truncated = False
    for line in (proc.stdout or "").splitlines():
        if len(matches) >= max_matches:
            truncated = True
            break
        # format: <relative-path>:<line>:<text>
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path_s, line_s, text = parts
        rel = path_s
        if rel.startswith("./") or rel.startswith(".\\"):   # rg may prefix "./"
            rel = rel[2:]
        rel = rel.replace("\\", "/")
        try:
            lineno = int(line_s)
        except ValueError:
            continue
        matches.append({"path": rel, "line": lineno, "text": text[:400]})
    return {"matches": matches, "truncated": truncated}
