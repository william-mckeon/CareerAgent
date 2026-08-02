---
name: tailor
description: Tailor the master resume to one specific job description.
---
# Tailoring playbook

Goal: produce a one-page resume aimed at ONE job, built only from the user's real evidence.

1. Read the job description. Pull out the role's top requirements, the must-have skills, and the
   language the employer uses (mirror their words where the user genuinely has the experience).
2. `search_projects` for the projects most relevant to THIS job, and use the master profile already in
   your context. A platform role and an ML role pull different projects from the same library — select,
   don't dump.
3. Draft the resume: a tight summary, a skills line (only skills the user actually has), 3–5 most-relevant
   projects/experience with strong action-verb bullets, education. Mirror the JD's language for the real
   overlaps.
4. Metrics: never invent a number. Where a bullet wants one the user hasn't given, write `[add metric]`
   and move on — do not estimate.
5. Gaps: if the JD requires skills the user's evidence doesn't show, leave them OFF and note ALL the gaps
   together in one short paragraph AFTER the draft. If the user says they DO have one, offer to add it to
   their profile first. Do not turn this into a question-per-skill.
6. SHOW the full resume in your reply, save it with `save_resume` to the application, and deliver — every
   run ends with the draft visible. Then finish.
