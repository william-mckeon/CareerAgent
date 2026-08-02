---
name: recommend-jobs
description: Find and honestly score real open roles against the user's full verified evidence, and recommend the best fits.
---
# Job recommendations playbook

Goal: surface real, currently-open roles that genuinely fit the user, each scored against their FULL
evidence — the master profile, their GitHub-reviewed projects, and their skills — with honest reasons.
An honest "this isn't a fit" is worth more than a flattering match.

## Build the picture (score against ALL of it, not one résumé line)
1. `read_profile` for the master profile, and `search_projects` for the user's real, GitHub-reviewed
   projects (languages, summaries, evidence — stronger signal than a résumé bullet).
2. Note the target roles, must-have skills, seniority, location/remote needs, and any stated preferences
   (industries/stacks to seek or avoid). If the target role is unclear, ask ONE clarifying question.

## Discover → read
3. Turn the profile into 2–4 targeted searches (role + seniority + "remote" + key skills / location) and
   run `web_search`. A result is a LEAD, not the job.
4. `fetch_url` the most promising 3–8 postings to read the ACTUAL job description. Only score a role you
   have real posting text for (from a fetched page or a JD the user pasted) — do not score from a snippet.

## Score each posting 0–100 against the evidence
- **Skills / stack match** — does the user's real profile + projects demonstrate what the role needs?
- **Seniority fit** — at or slightly above the user's level; avoid far-below/far-above.
- **Remote / location** — explicit remote, timezone, relocation/authorization constraints.
- **Freshness** — posted recently and still open (today's date is in your context); penalize stale/closed.
- **Preferences** — honor the user's stated likes/avoids.
- **Red flags** — requires on-site, an authorization the user doesn't meet, or core requirements entirely
  outside their evidence.
Give each: `score`, `verdict` (apply | review | skip), `match_reasons`, `red_flags`, and a one-line
`suggested_angle` (how to frame the application, from the user's real strengths).

## Grounding — the hard rules
- Every `match_reason` must point to evidence that is ACTUALLY in the profile or projects. Never claim the
  user is a fit on a skill, tool, or experience the dossier doesn't show — say it's a gap instead.
- Rank by honest fit, not optimism. It is fine (and useful) to recommend "skip" for a poor match.

## Recommend → act
Present the top matches ranked, each with its score, reasons, red flags, and angle. Then offer the
existing next steps for a chosen role — all already available to you:
- `create_application` to **track** it (pass the fetched JD as the job description),
- the `tailor` skill to draft a résumé for it, `ats_score` to check keyword coverage, and the
  `cover-letter` skill to draft a letter.
Show the ranked recommendations in your reply, then finish (don't apply on the user's behalf).
