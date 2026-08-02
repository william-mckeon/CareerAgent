# 0002 — careeragent-jobs: the recurring scheduler (#18b) + nightly repo pre-sync (Slice E)

> Turns TIME into WORK. A single in-process scheduler task seeds a small set of built-in recurring
> schedules and, every tick, enqueues the due ones into the same `jobs` table the worker drains. It
> **only enqueues** — the worker (0001) runs the handler. Two capabilities live here: the **#18b
> reminders** (follow-up-due / résumé-freshness scans) and the **Slice E nightly repo cache-warm**.

**Status:** shipped · **Builds on:** [0001-jobs.md](0001-jobs.md) · **Depends on:** careeragent-sessions
(the "🔔 Reminders" conversation) + careeragent-dossier (reminders) + careeragent-code (pre-sync).

---

## The model (no real cron)

A **schedule** is a Postgres row (`schedules` table): `{name UNIQUE, kind, spec, interval_seconds,
next_run, enabled, last_run}`. Cadence is a **fixed interval in seconds** plus a `next_run` watermark —
there is no cron expression, so "nightly" means `interval_seconds = 86400` from first seed (not a
wall-clock hour). Built-in schedules are declared in code (`scheduler.default_schedules(...)`) and seeded
**once** with `INSERT … ON CONFLICT (name) DO NOTHING`, so a redeploy never overwrites an operator's later
enable/disable/retune. There is **no** `POST /schedules` — users don't create schedules; adding a recurring
job = adding a built-in seed + a handler in the registry.

Every `tick_seconds` (default 60s) `_tick()`:
1. `store.due_schedules()` — enabled rows with `next_run <= now()`.
2. Resolve the singleton **"🔔 Reminders"** conversation (self-healing: reuse a persisted id, else adopt by
   title, else create). If sessions is unreachable, **defer** all due schedules this tick (never advance) —
   so an outage delays, never drops.
3. For each due schedule: `store.enqueue(kind, spec, cid)` then `advance_schedule(id, interval)` where
   `next_run = now() + interval` (not `last + interval`) — so a scheduler down for a day fires each schedule
   **once** on return, no catch-up storm.

## The recurring kinds

| kind | needs | cadence env | on run |
|---|---|---|---|
| `follow_up_scan` (#18b) | dossier | `JOBS_FOLLOW_UP_INTERVAL_SECONDS` | reminder message if any applications are follow-up-due, else `""` |
| `resume_freshness` (#18b) | dossier | `JOBS_FRESHNESS_INTERVAL_SECONDS` | reminder if any saved résumé is stale, else `""` |
| `repo_presync` (Slice E) | careeragent-code | `JOBS_PRESYNC_INTERVAL_SECONDS` | POST careeragent-code `/refresh`; always `""` (silent) |

A handler returning `""` means "ran fine, nothing to say" — the worker marks the job done and **skips the
inject**, so a quiet scan (or the silent cache-warm) never spams the Reminders conversation.

## Slice E — the nightly repo cache-warm

**Why.** A deep code review clones-or-pulls the repo on demand; the *first* review of the day pays a cold
clone. Pre-syncing overnight makes that first review warm. It is a **pure optimization** — on-demand review
is unchanged and always still pulls latest, so a warmed repo is never a staleness risk.

**How.** `handle_repo_presync` calls `careeragent-code /refresh` via a `CodeClient` (jobs' own
`client/code.py`, carrying **only `CODE_API_KEY`** — the GitHub PAT stays in careeragent-code). careeragent-code
discovers the user's owner repos with its PAT and clone-or-pulls each, **bounded** (a repo-count cap + a byte
budget so the sweep never evicts what it just warmed) and **fail-soft per repo**. See
[careeragent-code/specs/0001](../../careeragent-code/specs/0001-code-workspace.md).

**Silent + retry-correct.** On a `2xx` the handler returns `""` (silent success) even if the body reports
per-repo `errors` — a partial sweep is not re-run wholesale. Only a **total failure** (non-2xx / transport,
e.g. GitHub discovery down) **raises**, so the worker's `retry_or_fail` retries it; a transient outage just
no-ops until the next nightly tick.

## Start gate (fail-soft, per-kind)

The scheduler starts when the **worker** and **sessions** are up **and at least one** of {dossier, code} is
present. `default_schedules(follow_up, freshness, presync)` seeds a kind **only when its interval > 0**, and
the lifespan passes `0` for a kind whose client is absent — so a **dossier** outage never pauses the **code**
warm (and vice-versa), and no schedule is ever seeded whose handler could only fail.

The `repo_presync` job is enqueued targeting the Reminders conversation like any other schedule, but since its
handler returns `""` nothing is injected — a sessions blip only **defers** the warm (it is never posted).

## Auth / isolation

Jobs → careeragent-code carries **only `CODE_API_KEY`** (`X-API-Key`); the GitHub PAT never leaves careeragent-code
(ADR-011, amended). Same compartmentalized-key house pattern as the review/dossier/sessions boundaries.

## Config

`JOBS_SCHEDULER_ENABLED` (default true), `JOBS_SCHEDULER_TICK_SECONDS` (60), `JOBS_FOLLOW_UP_INTERVAL_SECONDS`
(86400), `JOBS_FRESHNESS_INTERVAL_SECONDS` (86400), `JOBS_PRESYNC_INTERVAL_SECONDS` (86400), `CODE_URL`,
`CODE_API_KEY` (empty → pre-sync disabled, no schedule seeded).

## Acceptance

- [ ] The built-in schedules seed once (`ON CONFLICT DO NOTHING`); a redeploy doesn't clobber a retune.
- [ ] A due schedule enqueues its kind and advances `next_run = now()+interval` (no catch-up storm).
- [ ] `repo_presync` fires `/refresh`, returns `""` on 2xx (nothing injected), and RAISES on a total failure.
- [ ] With careeragent-code unconfigured (`CODE_API_KEY` empty), no `repo_presync` schedule is seeded.
- [ ] With dossier down but code up, the scheduler still starts and seeds ONLY `repo_presync`.
- [ ] A sessions/GitHub outage defers/fails-soft; it never crashes the tick or double-posts.

---

*careeragent-jobs — the scheduler + nightly repo pre-sync. Part of the CareerAgent system. Port 8011.*
