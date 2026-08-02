# 0011 — Phase 7 #20: Plan-vs-Act (propose → confirm → execute)

> Promoted from the P7 scaffold (0009 #20). Give the coach a **read-only plan mode**: it investigates,
> then `propose_plan`s a structured approach and PAUSES for the user; on approval the same run resumes in
> **acceptEdits** with the agreed plan seeded as its checklist, and executes it.

**Status:** ✅ built — `propose_plan` + per-request mode + server-derived elevation; adversarially reviewed
(5 findings fixed, incl. a high-sev read-only-escape); live-verified (plan → propose → approve → execute;
decline & forged-mode stay read-only; free-text revision). · **Depends on:** P1 (`update_plan` checklist),
P4 (suspend/resume + approval buttons), the permission engine (`plan` / `acceptEdits`) · **Last updated:** 2026-07-21

## Shape (ratified)
- **Per-request `mode`.** Today `mode` is a fixed server-side `Config.PERMISSION_MODE` (deployed
  `acceptEdits`). #20 adds an OPTIONAL `mode` to the `/chat` request that the user selects
  (**Plan** / **Edit**), threaded frontend → careeragent-sessions → careeragent-api. Absent → the server
  default (today's behavior). This is the one genuinely new plumbing piece.
- **`propose_plan` — a CONTROL tool** (non-mutating; in the catalog in every mode, incl. plan). Args:
  `summary` + `steps: [{content}]` (same step shape as `update_plan`, so an approved plan seeds the
  checklist directly). It PAUSES the run (SOLO, like `ask_user`) with `pending_kind="plan_proposal"` and
  payload `{summary, steps}`, riding the existing P4 suspend frame + sessions run_state.
- **Approve → elevate + seed + execute.** On the resumed turn the api settles the `propose_plan` call
  (`_resolve_pending_plan`): granted → append a "you're in edit mode, execute it" tool result + **seed
  `plan` from the proposal's steps** + continue; declined → a "stay read-only, ask what to adjust" result.
  The elevation to `acceptEdits` is **server-derived** by careeragent-sessions from the granted
  `plan_proposal` (not client-asserted), mirroring how the write-approval path maps yes/no → `granted`.
  The frontend also flips its own Plan→Edit toggle so subsequent turns continue in edit mode.
- **Reuses, doesn't rebuild.** No new service, no DB migration — `plan_proposal` is just another
  `pending_kind` through the generic run_state; the plan is just `update_plan` steps.

## Wiring
- **careeragent-api:** `agent/tools.py` (`propose_plan` in `CONTROL_TOOLS` + `_CONTROL_SCHEMAS`);
  `agent/loop.py` (a `propose_plan` control intercept beside `ask_user`; `_resolve_pending_plan` +
  `_plan_steps_from_args`; route it before `_resolve_pending_approval` in the resume block; seed `plan`);
  `agent/prompts.py` (plan-mode guidance bullet); `backend/api.py` (`ChatRequest.mode` →
  `run_agent(mode=request.mode or config.PERMISSION_MODE)`).
- **careeragent-sessions:** `schemas.py` (`ChatRequest.mode`, `AnswerRequest.mode`); `client/api_client.py`
  (`stream_chat(..., mode)` → payload); `backend/api.py` (`_stream_turn(..., mode)`; `/chat` passes
  `request.mode`; `/answer` handles `plan_proposal` like `approval` and derives `mode="acceptEdits"` on grant).
- **careeragent-frontend:** `sse_decoder.py` (carry the full suspend `payload` on `pending`, so the plan's
  `steps`/`summary` reach the UI); `app.py` (a Plan/Edit `mode` toggle sent on every turn; a
  `plan_proposal` pending branch rendering the steps + **Approve & do it / Not now**; flip to Edit on
  approve); `conversations.py` (default mode on restore).

## Acceptance
- [x] In plan mode the coach cannot write; it analyzes and `propose_plan`s.
- [x] The proposal renders as a checklist with Approve / Not now; the free-text input still works
      (free text = a plan revision — the coach re-proposes, staying read-only).
- [x] Approve resumes the SAME run in acceptEdits, seeds the plan, and the coach executes the writes.
- [x] Decline keeps the run read-only and asks what to adjust; no write happens.
- [x] A forged/reset client `mode` can't execute a plan-mode write: the resume mode is **server-derived**
      from the run's persisted mode (only a genuinely-granted `plan_proposal` elevates it); the api also
      re-derives the gate and restricts the request `mode` to `plan|acceptEdits`.

## Non-goals
Multi-round plan negotiation UI beyond approve/decline+free-text; auto-executing without a click;
persisting the mode per conversation across a full page reload (session-local, like the P4 pending).

*careeragent-api — Phase 7 #20 (plan-vs-act). Part of the CareerAgent system. Port 8001.*
