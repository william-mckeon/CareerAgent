---
name: ats-check
description: Check a resume against a job description for keyword/ATS coverage.
---
# ATS keyword-coverage playbook

Goal: tell the user how well their resume covers the job's keywords, honestly.

1. Extract the concrete keywords from the JD: hard skills, tools, technologies, certifications, and role
   nouns. Ignore fluff ("team player", "fast-paced").
2. For each keyword, check whether the resume (and the user's real profile/projects) contains it.
3. Report: coverage as `matched / total`, the matched keywords, and the missing ones — grouped as
   (a) missing but the user HAS the evidence (add them to the resume) vs (b) genuine gaps the user does
   not have (do NOT add — that is fabrication; name them as gaps).
4. For (a), edit the resume to surface the real evidence using the JD's wording. For (b), suggest honest
   options (emphasize adjacent real skills; note a learning plan) — never invent the skill.
5. Deliver the coverage summary + the updated resume in your reply. Do not stall on questions.

If a `careeragent-ats` scoring tool is available, use it for the deterministic score, then explain it.
