# 0009 — Phase 7: Skills, artifacts & polish

> **SCAFFOLD** — agreed scope; each item finalized (and possibly split into its own spec) before it is
> built. The long tail — after core autonomy, dialogue, and reach are solid. Closes gaps
> **#10, #16, #17, #18, #19, #20**.

**Status:** scaffolded · **Depends on:** P1–P6 · **Last updated:** 2026-07-08

## Scope (each is independently shippable)

- **#10 Loadable skills + slash workflows** — ✅ **BUILT** (2026-07-20). Coaching playbooks as markdown
  files (`tailor`, `ats-check`, `quantify-bullets`, `cover-letter`) under `careeragent-api/src/agent/skills/`
  (baked into the image; `.dockerignore` negates the `*.md` exclusion for them). **Tri-modal** (Cline):
  *rules* stay in bio.txt/prompts.py; *skills* — `agent/skills.py` injects **only** the name+description
  INDEX into `build_system_prompt` (a `## Skills` section), and the coach loads a full body on demand via
  a new **read-only `use_skill` tool**; *workflows* — the frontend `/slash` menu (`app.py::_expand_slash`
  + a tip caption) expands `/tailor …` into a natural "use your tailor skill" prompt, sharing the same
  bodies. The momentum guard ("guidance for THIS task only — don't carry it across turns unless restarted")
  is in both the tool description and the injected section. Tests: `test_skills.py`.
- **#16 Artifact generation** — ✅ **BUILT** (2026-07-21; full contract + build notes in
  [`0010-artifacts.md`](0010-artifacts.md)). Two new deterministic microservices, each on the
  careeragent-fetch mold (no model/DB/egress). **Light half** — `ats_score(application_id)` READ tool →
  **careeragent-ats** (port 8010): keyword-coverage score of the SAVED résumé vs the JD (commit
  `175c962`; 7-finding review). **Heavy half** — `render_resume(application_id, format=pdf|docx)` WRITE
  tool → **careeragent-render** (port 8009): reportlab PDF / python-docx DOCX (commit `841ae4e`;
  8-finding review). Both resolve the SAVED text from dossier (never model-pasted, ADR-002). Binary
  transport: bytes live in dossier `resume_artifacts` (bytea), NEVER on the tool result / SSE content
  stream; `render_resume` returns a verified `{op:rendered_resume, artifact_id}` receipt + a
  `KIND_ARTIFACT` frame; download chain = frontend `st.download_button` → sessions passthrough → api
  download proxy → dossier. Turns "edited text in a DB" into "here's your polished PDF, and it covers
  8/12 JD keywords". Known limitation (ratified scope): the download button is session-local (survives
  reruns, not a full page reload) — follow-up documented in 0010.
- **#17 Agent-authored durable memory** — ✅ **BUILT** (2026-07-20). A `remember` WRITE tool writes
  user-*stated* preference notes ("targets senior PM", "metric-first bullets", "one page") to a
  dedicated **non-RAG** `preferences` table in careeragent-dossier (`0004_preferences.sql`), injected
  into `build_system_prompt` every turn as a `## Remembered preferences` section — **standing
  instructions, explicitly not evidence**. Distinct from careeragent-memory's automatic RAG. Rides the
  existing P3 ledger (the POST returns an `id` → verified receipt) + P4 approval gate; the completion
  gate also covers a "remembered your preference" over-claim. **Anti-laundering (ADR-002), the
  load-bearing invariant:** preferences are fetched into a **separate** variable and passed to
  `build_system_prompt` apart from `profile_content`, so they **never** enter the grounding corpus
  (`build_corpus_from_dossier` reads the profile, not preferences) — a stated preference can never back
  an invented resume claim. A stored preference is collapsed to a single line so it can't forge a
  markdown section in the system pin. Adversarially reviewed (anti-laundering + dossier clean). Tests:
  `careeragent-api/tests/test_preferences.py`.
- **#18 Background/async jobs + cron** — ✅ **BUILT** (#18a 2026-07-22, #18b 2026-07-21; full contract in
  [`0012-background-jobs.md`](0012-background-jobs.md)). A new microservice **careeragent-jobs** (port 8011
  + its own Postgres): an atomic job queue (`FOR UPDATE SKIP LOCKED`), a resilient worker (retries,
  startup requeue of crash-orphaned jobs, best-effort inject with retries), and the `review_repos` job
  kind (→ careeragent-review). The coach starts one via a new **`spawn_job`** control tool (gated by
  `permissions.decide` on the underlying write; fan-out-capped; fail-soft) and finishes the turn; the
  worker runs it and **injects the result** into the conversation via a new careeragent-sessions
  `POST /conversations/{id}/inject` — "do not poll". The frontend shows a "🔔 N background update(s)"
  badge. Adversarially reviewed (11 findings, all fixed). **#18b (cron/scheduler):** a second asyncio task
  (`scheduler.py`) seeds two recurring schedules (`follow_up_scan` — applications whose `next_follow_up`
  date has passed; `resume_freshness` — applications whose saved résumé is stale vs the master profile)
  in a new `schedules` table, and each tick ENQUEUES the due ones into a **singleton "🔔 Reminders"
  conversation** (id persisted in a `jobs_settings` k/v table). A handler returning the EMPTY string means
  "nothing due" → the worker skips the inject (no noise). Reminder kinds read a new **read-only
  DossierClient** (`GET /applications`, new `follow_up_due` filter). advance uses `now()+interval` (no
  catch-up storm); seeding is `ON CONFLICT DO NOTHING` (idempotent across redeploys). The frontend pins
  "🔔 Reminders" to the top. Adversarially reviewed (2 low findings — a Reminders-conversation fork window —
  both fixed via **reconcile-by-title dedup + best-effort persist**, making resolution self-healing).
  Live-verified end-to-end: scheduler creates the thread once, both scans run, `resume_freshness` injected
  a real 3-app reminder, `follow_up_scan` empty→skipped, the `follow_up_due` filter surfaced exactly the
  due app, and restart/mid-run re-resolution reused the persisted id with no fork.
- **#19 Typed structured streaming** — ✅ **BUILT** (2026-07-20). Typed SSE kinds (`plan_update`,
  `tool_start`, `tool_result`, `step`) ride the **same namespaced `careeragent` frame** as the P4 suspend,
  emitted by `loop.py::_typed(...)` **alongside** the existing plain-text `delta.reasoning` — purely
  ADDITIVE, so an un-upgraded frontend/careeragent-sessions just ignores them (no `choices` → skipped; not
  captured as content; never mistaken for a suspend). The frontend (`sse_decoder.py` new `KIND_*` +
  `SSEEvent.typed`, `app.py::_render_plan`) renders a **live plan checklist**; the tool frames populate the
  shared channel that **#16 (KIND_ARTIFACT) and #20 (plan_proposal) ride**. Adversarially reviewed (SAFE,
  no defects). Tests: api `test_typed_streaming.py`, frontend `test_sse_decoder.py`, sessions `test_chat.py`.
- **#20 Plan-vs-Act handoff** — ✅ **BUILT** (2026-07-21; full contract in
  [`0011-plan-vs-act.md`](0011-plan-vs-act.md)). A read-only **plan mode** (a per-request `mode`, threaded
  frontend → careeragent-sessions → careeragent-api) in which the coach investigates and calls a new
  control tool **`propose_plan`** (summary + steps) that PAUSES on the P4 suspend/resume channel
  (`pending_kind="plan_proposal"`). On **approve** the SAME run resumes in `acceptEdits` with the steps
  seeded as the `update_plan` checklist and executes them; **decline** stays read-only; **free-text** is a
  plan revision (the coach re-proposes). The elevation is **server-derived** (careeragent-sessions stamps
  the run's mode into the persisted snapshot and uses THAT — never the client's — on resume, so a
  reset/forged client mode can't escape read-only). Pairs with `ask_user` (P4) + `update_plan` (P1).
  Adversarially reviewed (5 findings, all fixed incl. the high-sev read-only-escape). Tests:
  `careeragent-api/tests/test_plan_vs_act.py`.

## Non-goals
None fixed — this is the backlog. Promote an item to its own numbered spec when it's next up.

*careeragent-api — Phase 7 (skills, artifacts, polish). Part of the CareerAgent system. Port 8001.*
