# 0004 — Phase 2: Loop hygiene

> A bundle of small, self-contained guards that keep the now-longer P1 loop from **dying** (a Bedrock
> throttle), **choking** (a malformed tool arg), or **spinning** (an identical call on repeat) on a
> weak model. Closes gaps **#5, #13, #14**, and folds in the P1 smoke-test wart (`finish_answer`
> emitted as a JSON blob). Contained to `careeragent-api` — no other service changes. See
> [`../../ROADMAP.md`](../../ROADMAP.md), ADR-005, ADR-008.

**Status:** ✅ built (as-built + review notes inline; passed a 4-lens adversarial review) · **Depends on:** P1 (0003) · **Unblocks:** P3 · **Last updated:** 2026-07-09

---

## Goal

The persist-until-done loop now runs longer and does more per turn. This phase makes it **robust**: a
single Bedrock 429/5xx must not dead-end the turn; a stringified-JSON arg must not silently become
`{}`; an identical call on repeat must not burn the step budget; and a `finish_answer` the model
emitted as *text* must reach the user as clean prose, not raw JSON.

## Current behavior (what we're hardening)

- `InfraClient.complete` is **single-attempt** (`client/infra.py`) — one 429/5xx raises, the loop
  catches it and streams "I couldn't reach the model" (`loop.py`). gpt-oss-120b on Bedrock *will*
  throttle. (Note: careeragent-infra already retries the *model* call; this adds a second, thin layer
  for the api↔infra hop + infra 5xx.)
- Tool args are parsed with a bare `json.loads(...)` and coerced to `{}` on failure (`loop.py`) — a
  malformed or stringified-JSON arg is silently dropped, so the tool runs with nothing.
- All tool calls in a step run **sequentially** in a `for` loop — reading profile + applications +
  projects together waits on three round-trips instead of one.
- No loop-detection: an identical call on repeat re-executes until the step budget.
- P1 wart: a plain reply that is a `finish_answer`-shaped JSON object (`{"summary": …}`) is streamed
  verbatim, so the user sees raw JSON (gpt-oss sometimes emits a tool call as text).

## Design

### #5a — Retry/backoff in `InfraClient.complete`
Add bounded exponential backoff **inside `client/infra.py`'s `complete()`** (covers both the main-loop
call and the P1 synthesis call in one place):
- Retry on: httpx transport errors (`ConnectError`, `ReadTimeout`, `RemoteProtocolError`), HTTP **429**,
  and **5xx**. **Never** retry other 4xx (a 422 is a bug, not a blip).
- Honor a `Retry-After` header when present; otherwise exponential backoff with jitter, capped.
- Bounded and **modest** (default 3 attempts) — infra already retries the model, so this layer only
  needs to ride out a transport blip or an infra 5xx, not stack minutes of latency.
- On exhaustion, re-raise — the loop's existing `except` still streams the user-facing fallback.
- Config via `Config` (api.py) → `InfraClient(...)` constructor: `MODEL_MAX_RETRIES`,
  `MODEL_BACKOFF_BASE`, `MODEL_BACKOFF_CAP`. Update the file's "no-retry" header rule to carve out
  `complete` (non-streaming; a fresh request, safe to retry — unlike the streaming `/chat`).

### #5b — Tool-arg validation + stringified-JSON coercion
A helper in `tools.py`, `coerce_and_check(tool_name, raw_args) -> (args, error|None)`, driven by the
tool's declared schema (add a `name → schema` lookup):
- If a param declared `object`/`array` arrived as a **string**, try `json.loads` it (gpt-oss emits
  `'{...}'`/`'[...]'` strings for structured params). On failure, keep it as-is (dispatch will error
  cleanly) — never crash.
- If a **required** param is missing after coercion, return a corrective error string
  ("missing required `application_id`") instead of a silent `{}`.
- Wire into `loop.py` right after arg-parsing: on `error`, append it as the tool result (a teaching
  message the model reads next step) and skip dispatch; else dispatch the coerced args. Applies to
  dossier/MCP tools; control tools keep their own light handling.

### #13 — Parallel independent reads
In `loop.py`'s tool-call handling: if **every** call in the batch is a non-mutating, non-control
**read**, dispatch them concurrently with `asyncio.gather` and append the results in original order.
Any batch that mixes reads with a control tool, a write, or a destructive call → **sequential**
(today's behavior). This captures the common win (the model reading profile + apps + projects at once)
with zero ordering risk on writes.

### #14 — Loop-detection (identical-repeat guard)
Track the signature `name + sorted-args-json` of each **executed real tool call**. If the very next
real tool call has the **same** signature, do **not** re-execute it — substitute a corrective result
("you already called `X` with identical arguments; try a different approach or call `finish_answer`")
and count it. After **2** identical repeats, break to the synthesis turn. Progressing calls with
*different* args (e.g. a plan checked off `4→3→2→1→0`) are **not** repeats and never trip this.
*(The "you didn't act / not done yet" nudge is already provided by P1's plan reminder — #14 here is the
loop-detection half only.)*

### P1 wart — unwrap a `finish_answer`-shaped JSON reply
In `loop.py`'s accept-plain-reply path: if `content.strip()` parses as a JSON **object** with a string
`summary` key, unwrap it via the existing `_finish_text(obj, "")` (so `open_items` are honored too) and
stream that; otherwise stream `content` unchanged. Only triggers on a whole-content single JSON object
with `summary` — a normal answer that merely *contains* JSON is untouched.

---

## Files touched

- `src/client/infra.py` — retry/backoff in `complete()`; header "no-retry" rule carve-out.
- `src/agent/tools.py` — `coerce_and_check` helper + a `name → schema` lookup.
- `src/agent/loop.py` — wire arg coercion/validation; parallel read dispatch (all-reads batch);
  loop-detection signature guard; `finish_answer`-JSON unwrap in the accept path.
- `src/backend/api.py` — `Config` retry knobs, passed to the `InfraClient(...)` constructor.
- `.env.example` — document `MODEL_MAX_RETRIES` / `MODEL_BACKOFF_*` (defaults).
- `tests/test_loop_hygiene.py` (new).
- *(possibly)* `tests/test_autonomy_loop.py` — only if parallel dispatch shifts observable tool ordering.
- **No other service changes.**

## Acceptance

- [ ] A simulated 429/5xx (then success) retries with backoff and the turn completes — no user-visible
      "couldn't reach the model"; a non-retryable 422 does **not** retry.
- [ ] A stringified-JSON object arg (`'{"a":1}'`) is coerced and dispatched; an unparseable one and a
      missing-required arg surface a corrective tool result, not a silent `{}`.
- [ ] An all-reads batch dispatches concurrently (results correct + in order); a batch with a write
      stays sequential and ordered.
- [ ] Two identical consecutive tool calls trigger a correction (second is not executed), and a
      persistent repeat breaks to synthesis — while a progressing `update_plan` (`4→3→2…`) never trips it.
- [ ] A `finish_answer`-shaped JSON plain reply is unwrapped to its `summary`; a normal answer is
      streamed unchanged.
- [ ] P1 behavior (spec 0003) and existing suites stay green.

## Non-goals

- **Verified-completion / grounding** — P3 (0005). This phase does not change what "done" means, only
  keeps the loop alive to get there.
- **The interactive channel** (ask_user/approval/resume) — P4 (0006).
- **Context compaction** — P6 (0008); the longer loop's context growth is bounded by the step budget
  for now.

## Design decisions

- **Why retry in `InfraClient.complete`, not the loop?** One home covers both the main call and the
  synthesis call, and keeps the loop readable. It does not violate the streaming `/chat` "no-retry"
  rule — `complete` is a fresh non-streaming request, safe to re-issue.
- **Why only *modest* retries?** careeragent-infra already retries the Bedrock model call; stacking a
  large retry budget here would multiply latency. This layer exists for the api↔infra transport hop and
  infra 5xx, so 3 attempts is enough.
- **Why parallelize only all-read batches?** Writes must stay ordered (an edit then a read of the same
  record); a mixed batch is rare and not worth the ordering risk. The common, safe win — reading
  several records at once — is captured cleanly.
- **Why signature-based loop-detection (not step counting)?** A weak model spins by repeating the
  *same* call; different-arg progress (a plan being checked off) is legitimate work. Keying on
  `name + sorted-args` distinguishes the two exactly.

## Review notes (as built — post adversarial review)

A 4-lens adversarial review (9 findings, 8 confirmed) tightened four things:
- **Retry catch set** broadened to the transient-transport family (`httpx.NetworkError` +
  Connect/Pool/Write timeouts + `RemoteProtocolError`) — the original hand-picked four missed
  `PoolTimeout` / `WriteError` / `ReadError`. `ReadTimeout` is deliberately **excluded**: on a
  non-streaming completion it means slow generation, and retrying just re-runs it.
- **Required-arg check** flags missing only when a key is absent or `None` — an empty string is a
  valid value (`edit_resume new_string=""` = delete the matched text), which the first cut wrongly
  rejected.
- **Loop-detection covers the parallel batch too** (a batch signature), and only a **successful**
  identical call counts as a spin — a retry after a *transient tool error* is allowed, not suppressed.
- **`_unwrap_finish_json`** only unwraps a genuine finish shape (keys ⊆ `{summary, open_items}`), so a
  real answer that happens to be a multi-key JSON object is never collapsed.

---

*careeragent-api — Phase 2 (loop hygiene). Part of the CareerAgent system. Port 8001.*
