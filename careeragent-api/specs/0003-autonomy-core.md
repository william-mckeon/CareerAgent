# 0003 — Phase 1: Autonomy core (persist-until-done loop + plan)

> Make the coach **finish the job**. Today `run_agent` ends the instant gpt-oss emits any prose, or
> punts at step 12. This phase flips the stop rule to **terminal-tool completion**, gives the loop a
> **plan/TODO** that drives continuation, raises the step budget to a **soft** ceiling, and replaces
> the punt with a **synthesis** turn. Self-contained in `src/agent/` — no other service changes.
> Closes gaps **#1** and **#3**. Enabler for the rest of the roadmap. See ADR-003, ADR-005.

**Status:** ✅ built (as-built notes inline; passed a 4-lens adversarial review) · **Depends on:** nothing · **Unblocks:** P2, P3, P4 · **Last updated:** 2026-07-08

---

## Goal

Convert "one tool call then stop" into "keeps working until the task is done or it needs the user."
Continuation must be **harness-driven** (open plan items force another step), not dependent on the
weak model choosing to volunteer another tool call.

## Current behavior (what we're changing)

- `DEFAULT_MAX_STEPS = 12` (`loop.py:41`), `for step in range(max_steps)` (`loop.py:127`).
- **First** no-tool-call turn is accepted as the final answer and streamed (`loop.py:155-159`).
- Step-12 exhaustion streams a fixed punt "…could you tell me a bit more about what you'd like to do
  next?" (`loop.py:194-196`).
- No plan/TODO anywhere; `plan` is only a permission *mode*, not a task-decomposition tool.

## Design

### 1. `finish_answer` terminal tool
A new tool the model calls to end the run deliberately. Schema:
```json
{ "name": "finish_answer",
  "parameters": { "summary": "string — the final answer to show the user",
                  "open_items": "string[] — anything still needing the user (optional)" } }
```
- Registered in `schemas_for_mode` for **every** mode; classified **non-mutating** in
  `permissions.decide` (works in plan/read-only mode).
- When called, the loop streams `summary` as the final answer and finishes.

### 2. Flip the stop rule
A no-tool-call turn is **no longer** automatically final (remove the auto-return at `loop.py:155-159`).
Instead:
- If `finish_answer` was called → finish (stream its `summary`).
- Else if the plan has **open items** (status `pending`/`in_progress`) → append a synthetic reminder
  message ("You have not finished. Continue the task, or call `finish_answer` when done. Open items:
  …") and loop again.
- Else (nothing open) → accept the prose as final (preserves today's behavior for simple one-shot
  answers). A **blank** reply falls through to the synthesis turn (§4) instead of streaming empty.
- **Reminder cap:** to avoid an infinite "keep going" loop against a stubborn model, cap consecutive
  reminders at `2` (`REMINDER_CAP`). **As built (diverges from the first draft):** once the cap is hit
  the loop **accepts the model's reply** — after two nudges it is clearly trying to respond, and forcing
  a synthesis would discard a real answer and cost a round-trip. A *blank* capped reply still routes to
  synthesis. The nudge counter resets only when a **non-control** tool ran, so a model that merely
  re-plans (`update_plan` only) still trips the cap. *(See Design Decisions.)*

### 3. Soft step budget
`DEFAULT_MAX_STEPS` 12 → **40**, made configurable via the existing `AGENT_MAX_STEPS` env var
(`api.py` Config; default raised 12 → 40). 12 was a task budget masquerading as a safety limit; 40 is a
safety ceiling.

### 4. Synthesis on exhaustion (replace the punt)
On hitting `max_steps` (or a blank capped reply), do **not** stream the canned punt. Make **one** final
tools-disabled `infra_client.complete` call with a `SYNTHESIS_PROMPT`: "Using only what you've already
done, give the best answer you can now. Summarize what you changed in the dossier and what still needs
the user. Use `[add metric]` placeholders for anything missing. Do NOT ask the user to continue." Stream
that. **Converse safety (as built):** because the synthesis turn disables tools (`tools: []`), the prior
turn's `tool_calls`/tool-result blocks are first **flattened to plain text** (`_flatten_for_synthesis`)
— Bedrock Converse rejects `toolUse`/`toolResult` blocks when no `toolConfig` is supplied, and infra
omits `toolConfig` when `tools` is empty. Without this the synthesis would 502 and degrade back into the
very punt it replaces (caught by the Phase-1 adversarial review). Never raises — degrades to a plain
"here's where things stand" line on failure.

### 5. `update_plan` tool + pinned plan
A tool the model uses to lay out and check off multi-step work:
```json
{ "name": "update_plan",
  "parameters": { "steps": "[{ id, content, status: pending|in_progress|completed|cancelled }]" } }
```
- Whole-list replacement (newest call wins — no merge logic; weak-model-friendly; confirmed by
  Cline `task_progress` and opencode).
- At most one `in_progress`. A passed/abandoned step is `cancelled`, **never** silently `completed`.
- Persisted on the conversation record in careeragent-sessions (same plumbing that already carries the
  transcript); **pinned into `convo` every step** (the coach already injects the whole profile each
  turn — reuse that mechanism) so the weak model cannot drop steps.
- Echoed on the `delta.reasoning` channel so the frontend can render a live checklist (typed events
  are a P7 refinement).
- **Completion binding (guard against ADR-002 fabrication):** a step reaching `completed` should be
  cross-checked against real dossier mutations in P3 (spec 0005). In P1, `completed` is model-set;
  P3 upgrades it to verified. Document the seam now so P3 slots in.

### 6. Persistence clause in the system prompt
Add to `prompts.build_system_prompt`: "Carry the task end-to-end. Don't stop at analysis or a plan —
execute it. When genuinely finished, call `finish_answer`. If you truly need the user to decide
something, say so plainly (a dedicated `ask_user` tool arrives in a later phase)."

---

## Files touched (as built)

- `src/agent/tools.py` — `_CONTROL_SCHEMAS` (`finish_answer` + `update_plan`), `CONTROL_TOOLS`;
  `schemas_for_mode` now returns control tools in **every** mode. Control tools are loop-handled, not
  dispatched to dossier.
- `src/agent/loop.py` — the stop-rule flip; `finish_answer`/`update_plan` interception; the reminder +
  cap with `did_real_work` gating; the soft budget; `_system_with_plan` (plan pinned into the system
  message each step); `_normalize_plan`/`_open_items`; `_flatten_for_synthesis` + `_synthesize`;
  `DEFAULT_MAX_STEPS` 12 → 40, `REMINDER_CAP = 2`.
- `src/agent/prompts.py` — the persistence guidance bullets; `SYNTHESIS_PROMPT`.
- `src/backend/api.py` — `Config.AGENT_MAX_STEPS` default 12 → 40 (already wired to `run_agent`).
- `.env.example` — documented `AGENT_MAX_STEPS` default 40 + the soft-ceiling/synthesis behavior.
- `tests/test_autonomy_loop.py` (new), `tests/test_tools.py` (updated catalog assertions).
- **`permissions.py` — NO change:** `finish_answer`/`update_plan` are non-mutating by default (not in
  `MUTATING`), so they are already allowed in every mode.
- **careeragent-sessions — NO change in P1:** the plan rides the in-turn `convo` (pinned into the system
  message); durable cross-turn run-state (and plan persistence) is **P4** (sessions spec 0002).

## Acceptance  *(all covered by `tests/test_autonomy_loop.py` unless noted)*

- [x] A multi-step request runs to completion across many steps instead of ending at the first prose
      turn. *(test_plain_reply_does_not_end_turn_while_plan_open)*
- [x] The loop ends on `finish_answer`, on a plain reply with nothing open (or after the nudge cap), or
      on the synthesis turn (budget exhausted / blank capped reply) — not on the first mid-task sentence.
      *(test_finish_answer_ends_the_turn, test_reminder_cap_accepts_reply_not_infinite_loop)*
- [x] `update_plan` stores a checklist, pinned into the system prompt every step; `cancelled` is
      representable and never auto-flips to `completed`; malformed args never wipe a live plan.
      *(test_plan_is_pinned_into_the_system_prompt, test_cancelled_step_is_not_open,
      test_malformed_update_plan_keeps_previous_plan)*
- [x] Hitting `AGENT_MAX_STEPS` produces a **synthesis** answer (what changed + what remains), not the
      old punt — and the synthesis request is Bedrock-valid. *(test_synthesis_on_step_budget_exhaustion,
      test_synthesis_is_bedrock_valid_not_a_degraded_punt)*
- [x] `AGENT_MAX_STEPS` overrides the default; unset → 40. *(api.py Config; `_run(max_steps=…)` in tests)*
- [x] `finish_answer` / `update_plan` work in read-only **plan** mode (non-mutating).
      *(test_control_tools_work_in_plan_mode; test_tools.py::test_control_tools_present_in_every_mode)*
- [x] Existing simple one-turn Q&A still returns in one step (no spurious reminder loop).
      *(test_simple_answer_still_returns_in_one_turn)*
- [x] No other service changes — P1 is contained to `careeragent-api/src/agent/` + config.

## Non-goals (this phase)

- **`ask_user` / mid-task questions** — needs the suspend/resume channel; **P4** (spec 0006).
- **Verified completion / grounding** — the `completed`→real-mutation binding is **P3** (spec 0005);
  P1 only leaves the seam.
- **Context compaction** — the longer loop can grow `convo`; compaction is **P6** (spec 0008). P1
  assumes sessions stay within budget for now.
- **Retry/backoff, arg-validation, loop-detection** — **P2** (spec 0004). P1 keeps today's
  single-attempt `complete` and teaching-error dispatch.

## Design decisions

- **Why terminal-tool completion instead of "keep going until no tool calls"?** A weak model emits
  prose constantly mid-task; ending on that is the bug. A deliberate terminal tool + a plan-driven
  reminder is the mechanism all four agents use, and it's "one `if`" in `loop.py` (Cline
  `agent-runtime.ts:604-712`).
- **Why a reminder cap, and why accept the reply at the cap?** Without a cap, a stubborn model that
  refuses to call `finish_answer` and has an open plan would nudge to the step budget every time. The
  first draft routed the cap into a synthesis; the review flagged that as worse UX — after two nudges
  the model *is* answering, and forcing a synthesis discards that real answer and costs a round-trip. So
  the as-built behavior **accepts** the capped reply (blank replies still route to synthesis), and the
  nudge counter resets only on a **non-control** tool so a re-planning-only model still trips the cap.
- **Why whole-list `update_plan` (not a diff)?** No merge/reconcile logic to get wrong — the newest
  list wins. Weak-model-friendly; a fourth-lineage-confirmed shape (Cline `task_progress`, opencode).
- **Why pin the plan, not trust the model to remember?** gpt-oss drops steps; pinning the open items
  each turn is what makes it finish. Same injection path as the always-injected profile.
- **Why 40, not unbounded?** opencode/Codex run effectively unbounded, but a hosted per-call cost and a
  finite context make a soft ceiling + synthesis the safer default; it can be raised via env.

---

*careeragent-api — Phase 1 (autonomy core). Part of the CareerAgent system. Port 8001.*
