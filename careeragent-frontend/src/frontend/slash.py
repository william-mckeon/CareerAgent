#!/usr/bin/env python3
# ============================================================================
# careeragent-frontend - /slash command expansion
# Maintainer: William McKeon
# ============================================================================
#
# ROLE:
#   Owns the /slash command vocabulary and its expansion into a natural request.
#   Keeping it here keeps app.py lean (mirrors how sse_decoder.py owns the SSE
#   protocol and conversations.py owns the conversation switcher).
#
# HOW IT WORKS:
#   Typing "/cmd <rest>" in the chat box expands into a natural-language request;
#   anything typed after the command is appended as task detail. There are two
#   flavors:
#     • skill-backed — the expansion says "Use your `X` skill", so the coach loads
#       the matching markdown playbook (careeragent-api/src/agent/skills/) and
#       follows it. These are the #10 coaching workflows.
#     • action / mode — invokes a capability directly (a background job, a tracker
#       scan) or selects a per-turn permission mode; no skill body.
#
#   expand_slash() returns (expanded_text, mode): `mode` is a per-turn permission
#   mode a command wants applied ('plan' for /plan), else None. app.py sets
#   st.session_state.mode from it before streaming the turn, so /plan enters the
#   real read-only plan mode (#20) — not just a prose "please plan first".
#
#   The playbook BODIES live in the api; this module only maps a slash to the
#   request that makes the coach act. Unknown text passes through untouched.
# ============================================================================

from typing import Dict, Optional, Tuple

# name -> {"expand": <prompt>, "mode": <optional per-turn permission mode>}
SLASH_COMMANDS: Dict[str, Dict[str, str]] = {
    # ------- skill-backed coaching playbooks (#10) -------
    "tailor": {"expand": "Tailor my resume to this job. Use your `tailor` skill."},
    "ats-check": {"expand": "Run an ATS keyword-coverage check on my resume against this job. "
                            "Use your `ats-check` skill."},
    "quantify-bullets": {"expand": "Quantify and strengthen these resume bullets. "
                                   "Use your `quantify-bullets` skill."},
    "cover-letter": {"expand": "Draft a cover letter for this job. Use your `cover-letter` skill."},
    "linkedin-review": {"expand": "Review my LinkedIn profile extensively. Use your `linkedin-review` skill."},
    "recommend-jobs": {"expand": "Find and score real open roles that fit me, and recommend the best. "
                                 "Use your `recommend-jobs` skill."},
    "deep-review": {"expand": "Do a DEEP, line-level review of my repo's ACTUAL code (not just the "
                              "README) and turn it into truthful, specific portfolio/résumé material. "
                              "Use your `deep-code-review` skill."},
    "content-ideas": {"expand": "Turn my real code/projects into grounded LinkedIn (or X/social) post ideas "
                                "AND draft the best one, mindful of what I've already posted. Use your "
                                "`code-content-ideas` skill."},
    # ------- action commands (invoke a capability directly, no skill body) -------
    "review-repos": {"expand": "Refresh my projects library from my GitHub repositories (a README-level "
                               "pass that files/updates a project card per repo). If most are already "
                               "up to date it's quick — do it now and summarize honestly what changed; "
                               "only hand it to a background job if it's genuinely slow. This refreshes the "
                               "library; it does NOT write posts — for that I'll ask for content-ideas."},
    "reminders": {"expand": "Check my application tracker right now: which applications are due for a "
                            "follow-up (their follow-up date has arrived or passed), and which saved "
                            "résumés are stale versus my current profile? Summarize what needs attention."},
    "fetch": {"expand": "Fetch this job posting from its URL and summarize the role, must-have "
                        "requirements, and keywords — then offer to track it as an application:"},
    # ------- mode command: force read-only plan mode for THIS turn (#20) -------
    "plan": {"expand": "Plan this task: investigate, then propose a step-by-step plan for my approval "
                       "before making any changes.", "mode": "plan"},
}


def expand_slash(text: str) -> Tuple[str, Optional[str]]:
    """Expand a leading /slash command into a request plus any per-turn mode it sets.

    Returns ``(expanded_text, mode)`` — ``mode`` is 'plan' for /plan, otherwise
    None. Text that isn't a known command is returned unchanged with mode None.
    Anything typed after the command is appended as task detail."""
    s = (text or "").strip()
    if not s.startswith("/"):
        return text, None
    name, _, rest = s[1:].partition(" ")
    name = name.strip().lower()
    cmd = SLASH_COMMANDS.get(name)
    if cmd is None:
        return text, None
    rest = rest.strip()
    expanded = cmd["expand"] + (f"\n\n{rest}" if rest else "")
    return expanded, cmd.get("mode")
