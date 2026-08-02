# 0006 — Phase 4: Interactive channel (ask · approve · resume · steer)

> **✅ BUILT** — one sessions-backed suspend/resume channel, four capabilities riding it, all
> live-verified and adversarially reviewed. Closes gaps **#2, #9, #12, #15**. See ADR-004. Sessions-side
> contract: [`../../careeragent-sessions/specs/0002-run-state-suspend-resume.md`](../../careeragent-sessions/specs/0002-run-state-suspend-resume.md).

**Status:** ✅ built · **Depends on:** P1 (0003) · **Last updated:** 2026-07-19

## As built (P4.1–P4.5 + review fixes)
Topology **frontend → careeragent-sessions → careeragent-api** (the api stays stateless; sessions holds
the durable run state and drives resume). Commits: `1bcd827` (P4.1 channel), `b3fc90e` (P4.2 ask_user),
`3982326` (P4.4 frontend), `91de6bf` (P4.3 approval), `f2cb735` (P4.5 steering), `4374858` (review fixes).

- **The channel (P4.1/4.2).** The coach pauses by emitting a namespaced **suspend frame**
  `{event:"suspend", pending_call_id, pending_kind, payload, snapshot:{convo,plan}}`; sessions catches it
  (`_extract_suspend`), persists the run **paused** with the snapshot, and forwards it. A later
  `POST /answer` validates `call_id` against the stored pending (a wrong/foreign id → 409) and **replays
  the saved snapshot convo** (never client-supplied) to resume. The suspended toolUse is left unanswered
  and resolved on resume before the next model call (Converse-safe).
- **#2 `ask_user` (P4.2).** A control tool `{question, options[2–5]}`, non-mutating (works in plan/default),
  must be **solo** (a batched call is nudged, not suspended — else siblings orphan). A free-text "Something
  else" is auto-added. Live-verified: pause → answer → resume, including a follow-up re-pause.
- **#12 approval (P4.3) — execute-on-approval.** `permissions.decide` returns **`needs_approval`** for a
  default-mode write / any destructive delete instead of the old "enable edit mode" dead-end. On "yes" the
  api **executes the exact pending call** under a one-shot grant (`_resolve_pending_approval`); on "no" it
  records a decline (hardened so the coach doesn't mis-narrate it as done). **Review fix:** the grant
  re-derives the non-granted gate and can never override a hard mode-deny.
- **#9 resume-on-reload (P4.4).** `conversations._rehydrate_pending` re-arms a paused question from
  `GET /run-state` after a page reload / sidebar switch.
- **#15 steering + interrupt (P4.5).** While a turn runs, `POST /steer` queues a message and `POST
  /interrupt` sets a flag against the **running** run_state row (a normal turn now `mark_running`s so
  there's something to target). The stateless coach polls sessions (fail-open `SessionsClient`) at the top
  of each step: an interrupt stops cleanly (`OUTCOME_INTERRUPTED`, no fabricated summary); steering rides
  the **system pin** (a user turn after tool results would be consecutive-user in Converse) — **fenced as
  untrusted user input** (review fix). Live-verified: a queued "reply STEERED" made the coach reply STEERED
  on an unrelated prompt.

**Frontend limitation (honest):** true *mid-stream* steering/Stop isn't possible in the Streamlit UI
(the script blocks during streaming); the capability is real at the API level (any non-blocking client),
and the interrupt still works via connection close. The UI change was intentionally skipped.

**Trust model (from the review).** Single shared per-service key; `conversation_id` is a UUID capability,
not a secret. `/answer` correctly adds `call_id` as a second required secret and is well-isolated;
`/steer` + `/interrupt` authorize on `conversation_id` alone (acceptable under the model — document, and
keep the api reachable only from sessions). **Deferred (low):** a `run_id`/generation guard on the single
`run_state` row (a delayed turn-N persist could clobber a newer turn-N+1 row — PLAUSIBLE, not reproduced);
and the interrupt/model-error text still lands in the transcript (the api strips the model-error
placeholder on replay, so it can't poison the model).

## Goal
Let the coach pause mid-task, hand control to the user (a question, an approval, or steering), and
**resume the same run** with the answer — instead of ending the turn and cold-restarting the loop.

## The one channel (build first)
`/chat` is a streaming HTTP request with no synchronous human channel, so implement suspend/resume
backed by careeragent-sessions: when the loop hits a suspension point it persists
`convo + step + pending_call_id (+ plan, partial drafts)`, streams a distinct typed SSE event, and
stops the generator cleanly. A later POST tagged for that `call_id` re-enters `run_agent` with the
saved state plus a synthetic `role:tool` message carrying the user's answer, and continues.

## The four capabilities on the channel
- **#2 `ask_user`** — tool `{question, options[2-5]}` (auto-inject a free-text "Other"); registered in
  every mode, **non-mutating** in `permissions.decide` (works in plan mode). Stream a `question` event;
  frontend renders clickable choices; resume on answer. Consider a `codex`-style auto-resolve timeout
  (proceed on best judgment) for idle/headless.
- **#12 Interactive permission approval** — when `permissions.decide` returns ask/destructive, stream
  an `approval-request` event ("Save this rewritten resume? / Delete this application?"), pause, and on
  "yes" record a one-shot grant in sessions that `decide()` honors for the next mutating call. Makes
  **default** mode usable instead of the "enable edit mode" dead-end (`permissions.py:121-127`); makes
  deletes safe without `bypass`.
- **#9 Resume interrupted work** — on a new turn for a session with an unfinished run, resume from the
  snapshot with a "you were mid-task; here's the plan and what's done — continue, don't duplicate"
  primer instead of cold-starting. On budget exhaustion, checkpoint-and-continue.
- **#15 Mid-run steering + interrupt** — frontend POSTs a steering message that sessions queues against
  the active turn; the loop drains the queue between steps and appends it before the next `complete`
  ("actually target the Staff role"). An abort signal stops the generator cleanly with an
  interrupted-turn marker.

## Acceptance
- [x] `ask_user` pauses the run, renders options in the frontend, and **resumes the same run** with the
      choice as a tool result (no cold restart, no lost drafts). *(live-verified P4.2/P4.4)*
- [x] `ask_user` works in plan/default modes (non-mutating). *(control-intercepted; test_interactive.py)*
- [x] A write in default mode prompts an approval, and proceeds only on an explicit "yes". *(P4.3;
      grant executes the exact call, decline skips it — live-verified)*
- [x] A reload / new turn for an unfinished session resumes the plan (the paused question re-arms via
      `_rehydrate_pending`), does not duplicate completed work. *(P4.4)*
- [x] A steering message injected mid-run changes the next step's behavior; interrupt stops cleanly.
      *(P4.5 — a queued steer made the coach reply "STEERED"; interrupt → `OUTCOME_INTERRUPTED`)*

## Non-goals
General subagents (P6), artifacts (P7). Do not build four separate mechanisms — one channel, four
consumers (ADR-004).

*careeragent-api — Phase 4 (interactive channel). Part of the CareerAgent system. Port 8001.*
