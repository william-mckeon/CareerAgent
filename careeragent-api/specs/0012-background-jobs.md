# 0012 — Phase 7 #18: Background / async jobs (+ cron)

> Promoted from the P7 scaffold (0009 #18). Let the coach start a SLOW task without blocking the turn:
> it enqueues a job, finishes the turn ("I'll post the results here when it's done"), and a background
> **careeragent-jobs** worker runs it and INJECTS the result back into the conversation — "do not poll".
> **Split build:** **#18a** (async jobs, this spec) ships first; **#18b** (cron / recurring reminders) second.

**Status:** ✅ **#18 fully built.** #18a — careeragent-jobs (worker + queue + `review_repos`) + `spawn_job`
+ inject; adversarially reviewed (11 findings fixed incl. stuck-'running' recovery, the add_message idx race,
and the default-mode approval bypass); live-verified. #18b — scheduler (`scheduler.py`) + `schedules`/
`jobs_settings` tables + seeded `follow_up_scan`/`resume_freshness` → singleton "🔔 Reminders" conversation;
adversarially reviewed (2 low fork-window findings fixed via reconcile-by-title + best-effort persist);
live-verified end-to-end. · **Depends on:** P5 (service mold), P6 (careeragent-review), P4 (careeragent-sessions
conversations), careeragent-dossier (the tracker the reminders read) · **Last updated:** 2026-07-21

## Shape (ratified)
- **New service careeragent-jobs (port 8011) + its own Postgres** (careeragent-jobs-db), on the
  careeragent-sessions mold. A `jobs` table (id, kind, spec jsonb, conversation_id, status, attempts,
  result, error, timestamps) + a **worker** background loop that claims a pending job
  (`FOR UPDATE SKIP LOCKED`), runs it by kind, and on success **injects the result** into
  `conversation_id`, with bounded retries on failure. NO agent loop, NO model — the worker calls leaf
  services directly.
- **`spawn_job` — a CONTROL tool** (careeragent-api). It enqueues a job to careeragent-jobs with the
  CURRENT conversation_id and returns immediately (the coach then `finish_answer`s). Gated: only in an
  EDIT mode (the work writes), only when careeragent-jobs is configured, only with a conversation to post
  back into — otherwise it nudges the coach to do the task inline. Fail-soft.
- **#18a job kind: `review_repos`** — the canonical slow task. The worker calls careeragent-review
  `/review-batch`, summarizes the counts, and injects "✅ Background repo review complete — reviewed N…".
- **Out-of-band injection** — a new careeragent-sessions `POST /conversations/{id}/inject {content, role}`
  appends the result as a message (no turn, no run_state change). The frontend surfaces it: the sidebar
  shows a **"🔔 N background update(s)"** badge when the server's message_count exceeds what's rendered
  locally, and a one-click reload pulls it in.

## Wiring
- **careeragent-jobs (NEW):** `src/{store,worker,jobtypes,schemas,security}.py`, `src/backend/api.py`,
  `src/client/{review,sessions}.py`; `database/{init.sql, migrations/0001_jobs.sql}`; Docker + compose
  (app :8011 + careeragent-jobs-db Postgres); docs + tests. `POST /jobs`, `GET /jobs/{id}`, `GET /jobs`,
  `GET /health`.
- **careeragent-api:** `client/jobs.py` (JobsClient — enqueue); `agent/tools.py` (`spawn_job` in
  `CONTROL_TOOLS` + schema); `agent/loop.py` (a `spawn_job` intercept beside `spawn_subagent` — gated,
  uses `conversation_id` + `jobs_client`; threaded through `run_agent`); `agent/prompts.py` (background-work
  guidance); `backend/api.py` (`JOBS_URL/JOBS_API_KEY/JOBS_ENABLED` config + fail-soft lifespan client).
- **careeragent-sessions:** `schemas.py` (`InjectRequest`); `backend/api.py`
  (`POST /conversations/{id}/inject` — appends a message; 404 if the conversation is unknown).
- **careeragent-frontend:** `conversations.py` (the update badge + a "check for updates" reload).

## Acceptance (#18a)
- [x] `spawn_job(kind='review_repos')` returns immediately; the turn finishes ("running in the background").
- [x] The worker runs the review and injects a result message into the conversation.
- [x] The frontend shows the injected message on refresh (badge + reload).
- [x] `spawn_job` is gated by `permissions.decide` (blocked in plan AND default/needs-approval) and fail-soft
      when careeragent-jobs is down (coach does it inline).
- [x] A bad job kind is a clear 400; a failing job retries then lands `failed` (no crash, no lost worker);
      a job orphaned in `running` by a crash/redeploy is requeued at startup; a transient inject failure retries.

## #18b — cron / recurring reminders (scheduler)
Time → work, on the SAME queue #18a drains. A second asyncio task in the careeragent-jobs lifespan
(`scheduler.py`) does NOT run handlers itself — it only ENQUEUES due jobs; the #18a worker runs them and the
empty-result skip keeps a quiet scan silent.
- **Two tables** (idempotent `ensure_schema` + `init.sql` + `migrations/0002_schedules.sql`): `schedules`
  (name unique seed-key, kind, spec jsonb, interval_seconds, next_run, enabled, last_run) and a `jobs_settings`
  k/v (holds the singleton `reminders_conversation_id`).
- **Two seeded schedules** (config cadence, default daily): `follow_up_scan` (dossier applications whose
  `next_follow_up` ≤ today) and `resume_freshness` (applications `stale` vs the master profile). Seeding is
  `ON CONFLICT (name) DO NOTHING` — redeploys never reset an operator's retune/disable or `next_run`.
- **The tick:** `due_schedules()` (enabled, `next_run ≤ now()`), resolve the "🔔 Reminders" conversation,
  `enqueue` a job per due schedule into it, then `advance_schedule` (`next_run = now()+interval` — NO
  catch-up storm). If the conversation can't be resolved, DEFER without advancing (retry next tick — a
  reminder is delayed, never dropped).
- **Reminder handlers** (`jobtypes.py`) read a new **read-only DossierClient** (`GET /applications` gained a
  `follow_up_due` filter + `next_follow_up` in the SELECT). Each returns a reminder STRING, or the EMPTY
  string when nothing is due → the worker marks the job done but SKIPS the inject.
- **Singleton "🔔 Reminders" resolution** (`ensure_reminders_conversation`, self-healing): reuse a persisted
  id that still 200s (reuse on a transient verify blip too — don't churn); else ADOPT an existing
  "🔔 Reminders" thread **by title** (dedup so a lost/unpersisted create can't fork a second thread); else
  CREATE one. Persistence is best-effort so a jobs-DB write outage can't orphan the new thread.

## #18b — wiring
- **careeragent-jobs:** `src/scheduler.py` (NEW), `src/client/dossier.py` (NEW, read-only);
  `src/store.py` (schedules + settings methods); `src/client/sessions.py` (`create_conversation`,
  `get_conversation`, `list_conversations`); `src/jobtypes.py` (`Deps.dossier` + `follow_up_scan`,
  `resume_freshness`); `src/worker.py` (skip inject on empty result); `src/schemas.py` (`ScheduleOut`);
  `src/backend/api.py` (dossier client + scheduler task gated on worker+sessions+dossier, config env,
  `GET /schedules`, health additions); `database/{init.sql, migrations/0002_schedules.sql}`; `.env(.example)`
  (`DOSSIER_URL/KEY` + `JOBS_SCHEDULER_*`/`JOBS_*_INTERVAL_SECONDS`); tests
  (`test_scheduler.py` NEW + jobtypes/worker/store/api additions).
- **careeragent-dossier:** `store.py` + `backend/api.py` — `search_applications` gained a `follow_up_due`
  filter and `next_follow_up` in the SELECT (read-only; backward-compatible additive field).
- **careeragent-frontend:** `conversations.py` — pins the "🔔 Reminders" conversation to the top of the
  sidebar (stable sort by title prefix).

## Acceptance (#18b)
- [x] Scheduler seeds two schedules once and creates the "🔔 Reminders" conversation on first boot.
- [x] A due schedule enqueues a job into the Reminders conversation, then advances `next_run` by one interval.
- [x] `resume_freshness` injected a real reminder listing the stale applications; `follow_up_scan` with
      nothing due returned empty → the worker skipped the inject (no noise).
- [x] The new dossier `follow_up_due=true` filter surfaced exactly the follow-up-due application.
- [x] Restart / mid-run re-resolution REUSES the persisted id — exactly ONE "🔔 Reminders" thread, no fork;
      seeding is idempotent (`next_run` unchanged across redeploys).
- [x] `GET /schedules` reports both schedules; health reports worker + scheduler running.

## Non-goals (#18 — out of scope for both halves)
A `schedule_reminder` tool that lets the COACH create ad-hoc schedules (the seeded cadence is fixed config
for now), and general async subagents (a background run of the full agent loop).

*careeragent-api — Phase 7 #18 (background jobs). Part of the CareerAgent system. Ports 8001 (api), 8011 (jobs).*
