# 0002 — Run-state suspend / resume (companion to careeragent-api P4)

> **✅ BUILT** — the sessions-side of the one suspend/resume channel powering `ask_user`, interactive
> approval, resume-of-interrupted-work, and mid-run steering. Consumer:
> [`../../careeragent-api/specs/0006-interactive-channel.md`](../../careeragent-api/specs/0006-interactive-channel.md).
> See careeragent-api ADR-004.

**Status:** ✅ built · **Depends on:** sessions 0001 · **Last updated:** 2026-07-19

## As built
A `run_state` table (one row per conversation; `init.sql` + `store.ensure_schema()` for a pre-P4 volume)
holding `{status, snapshot jsonb, pending_call_id, pending_kind, pending_payload, steer_queue,
interrupt_requested}`. Store: `save_run_state` / `get_run_state` / `clear_run_state`; `mark_running` (a
`/chat` turn now creates a running row so steering has a target); `resolve_pending` (atomically claims a
pending iff `status='paused' AND pending_call_id` matches, `SELECT … FOR UPDATE` then clear — a wrong/
double answer gets nothing → 409); `request_interrupt` + `drain_steer_and_flags` (read-then-clear the
queue + flag under a row lock, so an `UPDATE … RETURNING` can't hand back the post-clear values).
Endpoints: `GET /run-state`, `POST /answer` (branches on `pending_kind` — a **question** replays the
answer as a tool result, an **approval** sends the api a `{call_id, granted}` directive and lets the api
execute), `POST /steer` / `/interrupt` / `/drain-steer` (internal). `/chat` + `/answer` pass
`conversation_id` to the api so the coach can poll back. **Review fix:** the in-band error check now
matches every `[ERROR …]` shape (was `startswith("[ERROR]")`), so an errored turn isn't mis-persisted as
a clean assistant message. **Deferred (low):** no `run_id`/generation guard on the single row (a delayed
persist could clobber a newer run — PLAUSIBLE); `/steer` + `/interrupt` authorize on `conversation_id`
alone (accepted under the shared-key model).

## Goal
Persist enough of an in-progress `run_agent` turn that a later request can **resume the same run**
rather than cold-start it — one mechanism, four callers.

## Concepts (added)
- **run state** — a snapshot attached to a conversation: `{ status: running|paused|complete|interrupted,
  convo, step, plan, pending_call_id, pending_kind: question|approval, partial_drafts }`. One active
  run per conversation.
- **pending request** — when the coach pauses, the `pending_call_id` + `pending_kind` + the payload the
  frontend must render (question options / approval prompt). Resolved by a tagged reply.
- **steering queue** — user messages posted against an active run, drained by the coach between steps.

## Contract (HTTP, additive — existing `/chat` unchanged)
- `POST /chat` gains optional `{ answer_to_call_id, answer }` (resume a paused run) and `{ steer }`
  (queue a steering message). Absent → today's behavior.
- `GET /conversations/{id}/run-state` — the current run snapshot (or `none`).
- `POST /conversations/{id}/answer` — `{ call_id, answer }`; validates the `call_id` matches the active
  pending request (one user's reply can't settle another's); resumes.
- Resume rule: the answer re-enters careeragent-api `run_agent` with the saved `convo` + a synthetic
  `role:tool` message for `pending_call_id`; the coach continues from `step`.

## Store (additive to schema `careeragent_sessions`)
A `run_state` table (or a `jsonb` column on `conversations`) holding the snapshot + pending request +
steering queue. Crash-consistency: on resume, an interrupted run whose last message is a tool call with
no result is reconciled with a synthetic `is_error` (never assumed successful — ties to careeragent-api
ADR-006).

## Acceptance
- [x] A paused run persists its snapshot + pending request; `GET .../run-state` returns it.
- [x] `POST .../answer` with the correct `call_id` resumes the same run; a wrong/foreign `call_id` is
      rejected (409 — `resolve_pending` matches `status='paused' AND pending_call_id` under `FOR UPDATE`).
- [x] A steering message queues and is drained on the next step (live-verified: coach obeyed a queued steer).
- [x] An interrupted run stops cleanly (`OUTCOME_INTERRUPTED`); a declined approval never fabricates
      success, and a dangling tool call is answered (executed/declined) before the next model turn.
- [x] Existing `/chat` transcript behavior (0001) is unchanged when none of the new fields are sent
      (regression-checked: content still streams + persists).

## Non-goals (this spec)
The UI rendering of questions/approvals (careeragent-frontend) and the coach-side loop integration
(careeragent-api 0006) are separate. This spec only provides the durable run-state + resume API.

---

*careeragent-sessions — run-state suspend/resume. Part of the CareerAgent system. Port 8005.*
