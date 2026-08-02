# 0008 — Phase 6: Delegation & compaction

> **✅ BUILT** — make long and parallel work survivable: generalize the one hardwired fan-out into real
> read-only subagents, and compact the context so long sessions don't overflow gpt-oss's window. Closes
> gaps **#8, #11**. See ADR-001 (invest in the harness) and ADR-006 (the verified ledger the compaction
> boundary must respect).

**Status:** ✅ built · **Depends on:** P1 (0003), P3 (0005), P4 (0006) · **Last updated:** 2026-07-19

## As built

**Shape (ratified at P6 start).**
- **A lean `run_subagent` (agent/subagents.py), NOT a nested `run_agent`.** `run_agent` is a byte-stream
  generator welded to SSE, the P4 suspend paths, sessions steering, the P3 ledger, and the grounding/
  Guardian gates — none of which a synchronous subagent may have. `run_subagent` is a small non-streaming
  loop modeled on careeragent-review's `review_one`: a fresh convo `[role-system, task]`, a role-restricted
  read-only catalog, a bounded loop, and it returns the child's `finish_answer` text (or a salvage on
  budget). The parent appends that text as the tool result for the `spawn_subagent` call.
- **`spawn_subagent` is a CONTROL tool** (intercepted in loop.py beside finish_answer/ask_user), so it has
  every client + the `depth`/fan-out counters in scope. It is non-mutating and plan-mode-usable; the
  catalog is filtered to hide it when delegation is disabled or `depth >= MAX_DEPTH`.
- **Four READ-ONLY roles** (agent/roster.py): **bullet-critic**, **jd-gap-analyzer**,
  **company-researcher** (uses `fetch_url`; its output is fenced as advisory data), **reviewer** (quality/
  fit — complements, doesn't replace, the Guardian). Every role's catalog is `finish_answer` + `update_plan`
  + its read tools — never a write tool, `ask_user`, or `spawn_subagent`. **worker→reviewer** is the coach
  explicitly calling `spawn_subagent(role='reviewer')` on a draft (no automatic finish-gate).
- **Caps:** `MAX_DEPTH=1` enforced at the **schema level** (a child's catalog has no `spawn_subagent`, so
  it can't spawn); `MAX_FANOUT=3` per turn (a `subagent_calls` counter); child `max_steps=10`; child
  effort pinned cheap (**never** inherits the request's effort).
- **careeragent-review is UNCHANGED** — it stays the parallel HTTP fan-out with its own MCP + `commit_sha`
  idempotency; `review_repos` remains a dispatched write tool with its load-bearing ledger receipt.

**Compaction (agent/compaction.py).** A threshold-gated check at the top of each loop step (before the
`complete` call): `estimate_tokens` (prior response's `usage.prompt_tokens`, else a char heuristic that
counts the profile + tool schemas — the real overflow drivers). Over threshold → `compact()` summarizes
the oldest turns via a cheap tools-disabled `infra.complete` under `SUMMARIZER_PROMPT` and **drops** them,
cutting on a **user-message boundary** so the trimmed convo stays Converse-valid (starts with a user turn,
no orphaned tool results). The carry-forward is deterministic: the original request (pinned verbatim) +
the **verified ledger receipts** (echoed op labels only) + the running model summary, all pinned into the
system message. Fail-soft: any summarizer failure leaves the convo intact.

**The P3 ledger across the boundary — preserved by construction.** `ledger` is a separate structured list;
`compact()` never receives it and only rewrites `convo`; the completion gate reads the ledger, not convo
prose. `SUMMARIZER_PROMPT` also forbids asserting any save as done. So a summary can never launder an
unverified "completed" into durable state (regression-tested).

**Scope: intra-turn compaction (zero DDL, zero new endpoints).** Because the P4 suspend snapshot is
`{convo, plan}`, a compacted convo replays through resume unchanged. Cross-turn persisted compaction (the
literal spec phrasing "store in sessions") is **deferred** — the api is stateless and the frontend replays
full history every turn, so persistence needs a sessions endpoint + `run_state` column + reload; ratify
separately. On a compact→pause→resume the briefing/plan/ledger degrade gracefully (Converse-valid replay,
fail-closed re-challenge) — the documented, accepted behavior.

**Config (backend/api.py, all opt-in with the existing idiom):** `SUBAGENT_ENABLED/_MAX_DEPTH/_MAX_FANOUT/
_MAX_STEPS/_EFFORT`, `COMPACTION_ENABLED/COMPACT_TOKEN_THRESHOLD/COMPACT_KEEP_RECENT/COMPACT_EFFORT`.

## Goal
Give narrow subtasks their own clean context (keeps the weak main model focused), and let long
multi-application sessions keep running past the context budget.

## Acceptance
- [x] `spawn_subagent` runs a nested role-scoped agent and returns only its final text to the parent.
      *(test_subagents: finish-text return, role restriction, salvage, in-loop delegation.)*
- [x] A generated resume can be run through a reviewer subagent (draft→critique) before finishing.
      *(the `reviewer` role + the worker→reviewer pattern; test_coach_delegates_then_continues.)*
- [x] A long session that would overflow the window compacts oldest turns and keeps running, with the
      current request + plan preserved. *(test_compaction: fires over threshold, trims the payload,
      Converse-valid; briefing pins the original request.)*
- [x] The verified-completion ledger (P3) is respected across the compaction boundary (no laundering a
      model summary's "completed" into durable state). *(test_delegation_does_not_launder_a_write_claim;
      compact() never touches the ledger; SUMMARIZER_PROMPT forbids it.)*

## Non-goals (deferred)
Async/background subagents + cron (**P7**); write-capable roles; cross-turn persisted compaction (a
fast-follow); profile-delta injection; preserving the P3 ledger across a P4 *resume* (today's fail-closed
re-challenge is safe); reimplementing careeragent-review in-api.

*careeragent-api — Phase 6 (delegation & compaction). Part of the CareerAgent system. Port 8001.*
