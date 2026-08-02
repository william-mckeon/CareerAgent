#!/usr/bin/env python3
# ============================================================================
# careeragent-api - loadable coaching skills (P7 #10)
# ============================================================================
#
# Skills are coaching PLAYBOOKS — markdown files under agent/skills/ with a small
# front-matter header (name + description). Tri-modal, mirroring Cline:
#   - RULES: always-on persona guardrails (bio.txt + prompts.py) — NOT here.
#   - SKILLS: only the name+description INDEX is injected into the system prompt
#     every turn (cheap); the coach loads a full body ON DEMAND via the `use_skill`
#     tool (agent/tools.py). This keeps long procedures out of every turn.
#   - WORKFLOWS: a frontend `/slash` shortcut (careeragent-frontend/app.py) that
#     expands into a natural "use your <name> skill" prompt — it shares the SAME
#     body files but is user-triggered, not model-invoked.
#
# The bodies are trusted, author-written content baked into the api image (the
# Dockerfile COPYs src/), so they are NOT subject to the untrusted-input fencing
# that fetched pages / uploaded files get.
# ============================================================================

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)

# name -> {"name", "description", "body"}. Loaded once (skills are static in the image).
_CACHE: Optional[Dict[str, Dict[str, str]]] = None


def _parse(text: str) -> Dict[str, str]:
    """Split a skill file into its front-matter (name/description) + body."""
    m = _FRONT_MATTER.match(text or "")
    if not m:
        return {"name": "", "description": "", "body": (text or "").strip()}
    meta: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    return {"name": meta.get("name", ""), "description": meta.get("description", ""),
            "body": m.group(2).strip()}


def _load_all() -> Dict[str, Dict[str, str]]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    out: Dict[str, Dict[str, str]] = {}
    try:
        for fn in sorted(os.listdir(_SKILLS_DIR)):
            if not fn.endswith(".md"):
                continue
            try:
                with open(os.path.join(_SKILLS_DIR, fn), encoding="utf-8") as f:
                    parsed = _parse(f.read())
            except OSError:
                continue
            name = parsed["name"] or fn[:-3]
            parsed["name"] = name
            out[name] = parsed
    except FileNotFoundError:
        pass
    _CACHE = out
    return out


def skill_names() -> List[str]:
    return list(_load_all().keys())


def skills_index() -> str:
    """The name + description lines injected into the system prompt (NEVER the
    bodies — those load on demand). Empty string when there are no skills."""
    items = _load_all()
    if not items:
        return ""
    return "\n".join(f"- `{s['name']}` — {s['description']}" for s in items.values())


def load_body(name: str) -> Optional[str]:
    """The full playbook body for a skill, or None if there's no such skill."""
    s = _load_all().get((name or "").strip())
    return s["body"] if s else None
