#!/usr/bin/env python3
# ============================================================================
# careeragent-review - per-repo reviewer prompt + the submit_review contract
# ============================================================================
#
# Ported from OpenCode's _child_task (bounded, grounded, single-area reviewer),
# adapted for: (1) reading via the GitHub MCP instead of a local filesystem, and
# (2) STRUCTURED JSON output via a terminal `submit_review` function tool
# (careeragent-infra /complete has no native JSON mode — the model "returns" its
# answer by calling this tool, whose arguments map 1:1 onto dossier project
# fields).
# ============================================================================

from __future__ import annotations

REVIEWER_SYSTEM_PROMPT = """\
You are a technical reviewer extracting RESUME-WORTHY evidence from ONE GitHub \
repository, in isolation. You have read-only GitHub tools (mcp__github__*).

How to work:
- Start with the README. Then read package/manifest/config files (to identify \
the stack) and a few representative source files. Do NOT try to read the whole \
repo — target the files that reveal what it is and what was built.
- GROUND every claim in files you actually opened. If you could not read \
something relevant, say so in the summary rather than guessing.
- Focus on what a hiring manager cares about: what the project is, the user's \
role, the tech stack, and 2-4 concrete, evidence-backed accomplishments.

When you have enough to characterize the repo, call `submit_review` ONCE with \
the structured fields. That tool call IS your answer — do not write prose; the \
only thing that matters is the submit_review call. Keep each field tight."""

# The terminal "structured output" tool. Its arguments map onto dossier's
# project columns; the harness reads them and upserts the project.
SUBMIT_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_review",
        "description": (
            "Submit the finished structured review of this repository. Call this "
            "exactly once, when you have read enough to characterize the repo. "
            "This is your final answer — do not also write prose."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project display name (usually the repo name)."},
                "summary": {"type": "string", "description": "What it is and what the user built (2-4 sentences)."},
                "role": {"type": "string", "description": "The user's role on the project (e.g. 'Creator & lead engineer')."},
                "tech_stack": {"type": "string", "description": "Comma-separated stack, e.g. 'Python, FastAPI, Postgres, Docker'."},
                "highlights": {"type": "string", "description": "Markdown bullets: key accomplishments + concrete evidence."},
                "languages": {"type": "string", "description": "Languages/proportions if known, e.g. 'Python 82%, C++ 18%'."},
                "repo_url": {"type": "string", "description": "The repository URL."},
                "stars": {"type": "integer", "description": "GitHub star count, if known."},
            },
            "required": ["name", "summary"],
        },
    },
}

# Fields submit_review may set that map to dossier project columns (allowlist).
REVIEW_FIELDS = ("name", "summary", "role", "tech_stack", "highlights", "languages", "repo_url", "stars")


def build_task(repo_full_name: str, focus: str | None) -> str:
    """The user-turn task for one repo's reviewer."""
    owner, _, name = repo_full_name.partition("/")
    focus_clause = f" Focus specifically on {focus}." if focus else ""
    return (
        f"Review the GitHub repository {repo_full_name} (owner '{owner}', repo "
        f"'{name}').{focus_clause} Read it with the GitHub tools, then call "
        f"submit_review with the structured fields."
    )
