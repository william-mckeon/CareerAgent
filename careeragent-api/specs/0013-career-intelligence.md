# 0013 — Phase 8: Career Intelligence

> **SCAFFOLD** — agreed scope; each capability is finalized (and split into its own numbered spec) before
> it is built. The first phase *beyond* the P1–P7 harness: now that the foundation is complete (a
> persist-until-done coach, a verified/grounded trust gate, reach, delegation, artifacts, memory, async
> jobs + cron, plan-vs-act, web search), turn it OUTWARD — review the user's own presence and find the
> roles that actually fit. Closes no legacy gap; it is net-new product built ON the finished foundation.

**Status:** scaffolded · **Depends on:** all of P1–P7 · **Last updated:** 2026-07-23

## Why this exists

The coach can build and verify a résumé; it cannot yet (a) tell the user whether their public presence
is recruiter-ready, or (b) find and honestly score the roles worth applying to. Both are the same idea
pointed two directions, and both become *better* here than in a standalone tool because CareerAgent scores
against a **verified, multi-source foundation**, not one flat résumé file:

- the **master profile** (careeragent-dossier),
- the user's **GitHub-reviewed projects** — real repos/languages/summaries filed by `review_repos` (evidence, not a bullet),
- **skills + stated preferences** (`remember` → the preferences table),
- and every claim runs through the **P3 grounding + Guardian gate** — so a "strong match on your Kubernetes
  work" reason is *checked* against whether the dossier actually evidences Kubernetes. The output is honest.

Inspiration (folder-reviewed, **no code copied**): a colleague's `ai-job-scraper` — its one good pattern is
*ingest → structured-extract → LLM-score-with-reasons → actionable output*. We reimplement that pattern on
our own stack and our richer evidence; we do **not** adopt its scraping (see ADR-010).

## Scope (three capabilities, each independently shippable)

- **#21 LinkedIn import** — get the user's OWN LinkedIn profile into the dossier. ToS-clean paths only:
  a "Save to PDF" of their profile (→ the existing careeragent-fetch `/extract`), or the "Get a copy of
  your data" export ZIP (→ a new careeragent-fetch `/linkedin/import` that parses the CSVs in the same
  isolated subprocess the résumé upload uses). A public-URL `fetch_url` is best-effort only (LinkedIn
  usually walls a datacenter fetch). **No scraping, no cookie/session, no third-party scraper API.** Full
  contract: [`0014-linkedin-profile.md`](0014-linkedin-profile.md) · fetch side: [`../../careeragent-fetch/specs/0002-linkedin-import.md`](../../careeragent-fetch/specs/0002-linkedin-import.md).

- **#22 LinkedIn review** — an EXTENSIVE, structured audit of the imported profile: headline, About,
  each experience (impact/quantification/keywords), skills coverage vs target roles, completeness gaps,
  recruiter-SEO (benchmarked with `web_search` + `ats_score`-style keyword coverage), **consistency vs the
  dossier/résumé**, and red flags — each scored, with a concrete grounded rewrite and a prioritized action
  list. A loadable `linkedin-review` skill drives it; a `/linkedin-review` slash command invokes it.
  Grounding fit: it critiques and rewords the user's REAL content; a missing skill is a **gap**, never an
  invented claim (exactly how the P3 gate already behaves). Contract in [`0014-linkedin-profile.md`](0014-linkedin-profile.md).

- **#23 Job recommendations** — the colleague's headline feature, reimagined on the foundation:
  derive queries from the dossier → `web_search` (P7) → `fetch_url` the shortlist → score each posting
  0–100 against the FULL evidence (grounded: no flattering fit) with `match_reasons` / `red_flags` /
  `suggested_angle` / verdict → recommend, and for a good match run the *existing* downstream: `tailor`,
  `ats_score`, cover-letter, and `create_application` to track it.
  - **Phase 1 (on-demand):** a `recommend-jobs` skill + `/recommend-jobs` — coach-driven, reuses existing tools only.
  - **Phase 2 (recurring/autonomous):** a headless careeragent-api `POST /recommend` + a careeragent-jobs
    `recommend_jobs` kind on the #18b scheduler → a weekly "5 new roles that match you" drop into the
    **🔔 Reminders** conversation, each pre-scored. Contract in [`0015-job-recommendations.md`](0015-job-recommendations.md).

## How they compose on the foundation (no new microservice, no scraper)

```
        careeragent-dossier (master profile + GitHub projects + skills + preferences)
                       │  the evidence everything scores against
   ┌───────────────────┼─────────────────────────────────────────────┐
   ▼                   ▼                                               ▼
#21 LinkedIn import   #22 LinkedIn review (skill)                #23 Job recommendations (skill / job)
 fetch /extract        read_profile + web_search + ats_score       web_search → fetch_url → grounded score
 fetch /linkedin/import   → grounded audit + rewrites → render      → recommend → tailor/ats/cover/track
                                                                     → (Phase 2) careeragent-jobs weekly scan
```

Every arrow is an existing capability. The genuinely new code is two **skill rubrics** (the product), the
`/linkedin/import` parser, and — for Phase 2 only — a headless `/recommend` endpoint + a `recommend_jobs`
job kind. The keyword gaps #23 surfaces feed straight into #22's rewrites, and vice-versa.

**Deeper still — #24 (spec [0016](0016-deep-code-review.md)):** the coach's repo review today is a
portfolio *summarizer* (a 6 KB-capped MCP read → a project card). A read-only **code workspace**
(`careeragent-code`) gives it a real local checkout so it can review repos at the LINE level, filing much
richer, evidence-backed project entries — which makes #22's résumé claims and #23's job-match scoring
sharper, and powers code-grounded content ideas ("review my repos vs my X posts → post ideas"). Read-only,
no-exec, PAT-isolated (ADR-011).

## Non-goals

- **Scraping LinkedIn** (cookie/session, or a third-party scraper API). ToS + account-ban risk; ADR-010.
- **A new microservice.** All of Phase 8 lands as skills + light wiring on the existing services.
- **Auto-applying to jobs.** The coach recommends, tailors, and tracks; the human applies.
- **Ungrounded scoring.** A recommendation/review may never assert a fit on evidence the dossier lacks.

## Acceptance (umbrella)

- [ ] The coach reviews an imported LinkedIn profile and returns a scored, sectioned audit with concrete
      grounded rewrites — inventing nothing (missing skills surface as gaps).
- [ ] The coach recommends real, currently-open roles scored against the master profile + GitHub projects,
      with honest `match_reasons`/`red_flags`, and can tailor + ATS-score + cover-letter + track a chosen one.
- [ ] No scraping path exists in the codebase; the LinkedIn content comes only from the user's own upload
      (PDF/ZIP) or a best-effort public `fetch_url`.

*careeragent-api — Phase 8 (career intelligence). Part of the CareerAgent system. Port 8001.*
