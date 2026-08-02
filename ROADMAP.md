# CareerAgent — Capability Roadmap

> The plan of record for closing the gap between the CareerAgent coach and a real agent.
> Twenty capability gaps, grouped into **seven dependency-ordered phases (P1–P7, now complete)**, plus
> **P8 · Career Intelligence** — the first net-new phase built on the finished foundation. This file is
> the index; each phase has a numbered spec under `careeragent-api/specs/`. Flip the **Status** column
> here as phases land so the plan never drifts from the code.

**Status:** P1–P7 complete · P8 in progress · **Last updated:** 2026-07-23 · **Maintainer:** William McKeon

---

## Why this exists

The coach today is a single-shot, reactive assistant. `careeragent-api/src/agent/loop.py`
runs a bounded 12-step tool loop that **ends the instant gpt-oss-120b emits any prose**
(`loop.py:155-159`) or dead-ends at step 12 with a canned "tell me more about what you'd like
to do next" punt (`loop.py:194-196`). Continuation depends entirely on the weak model choosing
to volunteer another tool call — nothing in the harness forces it forward. So "rewrite all 6
bullets to match this JD" gets abandoned the moment the model emits a sentence, and the user
re-drives it in a fresh loop from step 0. It cannot ask the user a clarifying question
mid-task, has no plan/todo, no general delegation, and no reach beyond the dossier (no
JD-from-URL, no PDF ingestion).

Four mature agents were reviewed folder-by-folder to find what they can do that our coach
cannot — first through a three-bug hardening lens (v1–v4 port plans), then a pure
**capability-gap** lens. All four converged. The gaps and the plan to close them are below.

- Full gap analysis + baseline: [`careeragent-api/specs/0001-capability-gap-analysis.md`](careeragent-api/specs/0001-capability-gap-analysis.md)
- Cross-cutting decisions (ADR log): [`careeragent-api/specs/0002-architecture-decisions.md`](careeragent-api/specs/0002-architecture-decisions.md)

**Provenance (reviewed):** openagent_code (Python), opencode (SST, TypeScript), Codex (OpenAI,
Rust), Cline (TypeScript). Same-stack **openagent_code is the closest copy-paste template**;
concepts from the others are reimplemented in Python, never lifted.

---

## The phases

> P1–P7 closed the original 20 capability gaps (the harness). **P8 is the first phase *beyond* the
> original plan** — net-new product built on the finished foundation, not a gap-closer.

| Phase | Spec | Bundles | Why one phase | Status |
|---|---|---|---|---|
| **P1 · Autonomy core** | [0003](careeragent-api/specs/0003-autonomy-core.md) | persist-until-done loop + plan/todo | the loop that finishes; enabler for the rest | ✅ built |
| **P2 · Loop hygiene** | [0004](careeragent-api/specs/0004-loop-hygiene.md) | retry/backoff · arg-validation · loop-detection · parallel reads · finish-JSON unwrap | all small; keep the longer loop alive | ✅ built |
| **P3 · Trust gate** | [0005](careeragent-api/specs/0005-verified-completion-and-grounding.md) | verified-completion ledger + grounding gate + fail-closed Guardian verifier | one guard; highest trust payoff | ✅ built (slices 1–4) |
| **P4 · Interactive channel** | [0006](careeragent-api/specs/0006-interactive-channel.md) | ask_user · permission approval · resume · steering | **one** sessions pause/resume build → four capabilities | ✅ built (all four capabilities live-verified; adversarially reviewed) |
| **P5 · Reach** | [0007](careeragent-api/specs/0007-reach-and-ingestion.md) | fetch-JD-from-URL · PDF/DOCX ingestion | get real input in | ✅ built — careeragent-fetch (SSRF egress + isolated PDF/DOCX extract), `fetch_url` read tool, resume upload; adversarially reviewed (5 fixes); live-verified (SSRF+CGNAT blocks, real fetch, extract via isolated subprocess, api↔fetch + frontend↔fetch) |
| **P6 · Delegation + compaction** | [0008](careeragent-api/specs/0008-delegation-and-compaction.md) | general subagents · context compaction | long/parallel work survivable | ✅ built — `spawn_subagent` (4 read-only roles, lean run_subagent, depth/fan-out caps) + intra-turn compaction (threshold-gated, ledger-safe); adversarially reviewed (3 fixes); 216 tests; live-verified (real coach delegated to a reviewer subagent end-to-end, outcome=final, 0 errors) |
| **P7 · Polish / product** | [0009](careeragent-api/specs/0009-skills-artifacts-polish.md) | skills/slash · render+ATS artifacts · memory · cron · typed streaming | the long tail (backlog — ship item by item) | ✅ built — ✅ #17 durable memory (`remember`), ✅ #19 typed structured streaming, ✅ #10 loadable skills + `/slash`, ✅ #16 artifacts (careeragent-ats `ats_score` + careeragent-render `render_resume` PDF/DOCX), ✅ #20 plan-vs-act (`propose_plan` → approve → execute; per-request mode), ✅ #18a async jobs (**careeragent-jobs** worker + queue + `spawn_job` "do not poll" → injected results), ✅ #18b cron / recurring reminders (scheduler + seeded `follow_up_scan`/`resume_freshness` → singleton "🔔 Reminders" conversation; adversarially reviewed) — **P7 COMPLETE** |
| **P8 · Career Intelligence** | [0013](careeragent-api/specs/0013-career-intelligence.md) | LinkedIn import · LinkedIn review · job recommendations · deep code review ([0016](careeragent-api/specs/0016-deep-code-review.md)) | turn the finished foundation OUTWARD — review the user's presence + code + find roles that fit, scored against verified evidence | 🚧 in progress — ✅ #22/#23 on-demand review + recommendations (`a009e6b`); net-new on P1–P7; grounded scoring; own-data-only LinkedIn (ADR-010); read-only code workspace (ADR-011); inspired by (no code from) `ai-job-scraper` |

**Already shipped (related):** anti-fabrication persona hardening — commit `08507de`. See
0002 · ADR-002.

---

## Build order (dependency-forced)

1. **P1 first.** Self-contained in `loop.py` + `prompts.py` + one new `finish_answer` tool.
   Converts "one call then stop" into "finishes the job." Everything else is easier once the
   loop persists.
2. **P2** right after — item-driven continuation needs the plan/todo from P1.
3. **P3** — the trust gate needs the loop to run long enough to actually make edits.
4. **P4** — the big infra investment: build the sessions-backed pause/resume channel **once**,
   then land `ask_user`, interactive approval, resume-of-interrupted-work, and mid-run steering
   on it. Companion spec: `careeragent-sessions/specs/0002-run-state-suspend-resume.md`.
5. **P5 → P6 → P7** — reach, then scale, then polish.

---

## The 20 gaps → phase map

| # | Gap | Family | Priority | Phase | Present in |
|---|---|---|---|---|---|
| 1 | Persist-until-done autonomous loop | looping | critical | P1 | all 4 |
| 2 | Ask the user a question mid-task, resume same run | ask-questions | critical | P4 | all 4 |
| 3 | Explicit plan / TODO the loop pins each turn | planning | high | P1 | all 4 |
| 4 | Verified-completion / anti-fabrication gate | self-correction | high | P3 | oa, cline |
| 5 | Retry transient failures + validate tool args | self-correction | high | P2 | all 4 |
| 6 | Fetch a job posting from its URL + web search | research | high | P5 | all 4 |
| 7 | Ingest an uploaded PDF/DOCX resume | attachments | high | P5 | all 4 |
| 8 | General subagent delegation + worker→reviewer | delegation | high | P6 | all 4 |
| 9 | Resume interrupted multi-step work | resume | high | P4 | all 4 |
| 10 | Loadable skills + slash-command workflows | skills | medium | P7 | all 4 |
| 11 | Context compaction for long sessions | resume | medium | P6 | all 4 |
| 12 | Interactive per-action permission approval | ask-questions | medium | P4 | all 4 |
| 13 | Parallel tool execution within a step | other | medium | P2 | all 4 |
| 14 | Loop/repeat detection + "you didn't act" nudge | self-correction | medium | P2 | cline, oc |
| 15 | Mid-run steering + clean interrupt | looping | medium | P4 | oc, cx, cl |
| 16 | Render a formatted resume (PDF/DOCX) + ATS score | other | medium | P7 | all 4 |
| 17 | Agent-authored durable memory (preferences) | other | low | P7 | oa |
| 18 | Background/async subagents + cron jobs | delegation | low | P7 | oc, cx, cl |
| 19 | Typed structured streaming progress | streaming | low | P7 | cx, cl, oc |
| 20 | Plan-vs-Act propose→confirm→execute handoff | planning | low | P7 | cline, oc |
| 21 | Import the user's OWN LinkedIn profile (PDF/export; no scraping) | career-intel | high | P8 | ai-job-scraper (concept) |
| 22 | Extensive, grounded LinkedIn profile review | career-intel | high | P8 | ai-job-scraper (concept) |
| 23 | Job recommendations vs verified evidence (on-demand + recurring) | career-intel | high | P8 | ai-job-scraper (concept) |
| 24 | Deep code review off a local checkout (careeragent-code) + code-grounded content ideas | career-intel | high | P8 | — (a real gap: MCP review is a 6 KB-capped summarizer) |

_(oa = openagent_code, oc = opencode, cx = Codex, cl = Cline; **P8 gaps #21–#23 are net-new**, inspired
by a colleague's `ai-job-scraper` — concept only, no code copied. See [0013](careeragent-api/specs/0013-career-intelligence.md).)_

---

## How to use this file

- **Every phase has a spec; the spec is the contract.** Do not code ahead of the spec.
- Write P1's spec (0003) in full before starting P1. Scaffold specs 0004–0009 carry the agreed
  scope + acceptance criteria now; fill their detail **just-in-time** before each phase — writing
  all seven in full upfront would itself drift once P1 teaches us things.
- **Flip a phase's Status here** (🔲 planned → 🚧 in-progress → ✅ done) as it lands, and link the
  commit. This table is the drift check.

---

*CareerAgent — capability roadmap. Phase specs live in `careeragent-api/specs/`.*
