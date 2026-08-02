---
name: linkedin-review
description: Extensively review the user's own LinkedIn profile — a scored, sectioned audit with concrete grounded rewrites.
---
# LinkedIn review playbook

Goal: an EXTENSIVE, honest audit of the user's OWN LinkedIn profile — scored section by section, with a
concrete rewrite for each weak spot and a prioritized action list. Ground everything in what the profile
and the dossier actually show; never invent experience.

## Get the profile
The profile text comes from the user, not from scraping. In order of preference:
1. A LinkedIn **PDF/export they uploaded** — its extracted text is in this conversation (fenced as their
   own document). Use that.
2. If none is present, ask them to add it: *"Save to PDF"* from their profile, or *Settings → Data
   Privacy → Get a copy of your data* (tick the profile categories) — then upload it.
3. A **public profile URL** they give you: try `fetch_url` once. LinkedIn often walls it (login page /
   error) — if the fetched text isn't a real profile, don't guess; ask for the PDF instead.

Also `read_profile` (and `search_projects`) so you can check the LinkedIn profile against the user's REAL
master evidence — consistency is part of the audit.

## Audit — score each section 0–100, note strengths + gaps + a rewrite
Work through ALL of these; a missing/empty section is itself a finding:
1. **Headline** — a keyword-rich value proposition, or just a job title? Does it name the target role and a differentiator?
2. **About / Summary** — a hook in line 1, a short narrative, the keywords a recruiter searches, a call to action. Flag walls of text and empty buzzwords.
3. **Experience** (each role) — achievement-led, not duty-listing? **Quantified impact**? The employer's keywords? Recency and gaps. Judge each role, not just the latest.
4. **Skills** — do they cover the user's target roles? Are the most important ones first? Note skills the dossier evidences that are missing from LinkedIn.
5. **Education / Certifications / Licenses / Featured / Recommendations** — completeness and social proof; note what's absent.
6. **Recruiter-SEO** — does the profile contain the terms recruiters actually use for the user's target role? Benchmark it DIRECTLY: `web_search` a couple of live postings for that role (or use a saved application's JD) and compare their must-have keywords against the PROFILE text. Report the high-value keywords the profile is missing that the user could TRUTHFULLY add. Do NOT use `ats_score` here — it scores a saved *résumé* against a JD, not the LinkedIn profile, so its number is about the wrong document.
7. **Consistency vs the dossier/résumé** — do titles, dates, employers, and claims on LinkedIn match the master profile? Flag every discrepancy — a recruiter will notice, and an inconsistency is a credibility risk.
8. **Red flags** — typos, first-/third-person drift, unexplained gaps, dead links, a default photo/banner, keyword stuffing.

## Grounding — the hard rules
- Critique and REWORD the user's real content; you may sharpen wording, add their real keywords, and
  restructure — you may NOT add experience, skills, employers, metrics, or claims the evidence doesn't show.
- A skill the target role wants but the user's evidence lacks is a **gap** — list it as a gap to consider,
  never as something to put on the profile. If they say they DO have it, offer to add it to their profile first.
- Never invent a metric for a rewrite. Where a stronger bullet wants a number the user hasn't given, write `[add metric]`.

## Output
Lead with an **overall score (0–100)** and a one-line verdict. Then, per section: `score`, `strengths`,
`gaps`, and a ready-to-paste **rewrite** (or "looks strong — no change"). End with a **prioritized action
list** — the 3–5 highest-impact fixes first. Show the whole review in your reply. Then offer to draft the
rewritten headline/About in full, or to save the key takeaways with `remember`, and finish.
