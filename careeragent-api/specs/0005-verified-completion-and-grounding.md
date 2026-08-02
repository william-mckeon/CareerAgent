# 0005 — Phase 3: Verified-completion + grounding gate

> The **trust layer**: make "I saved your resume" and "here's your rewritten bullet" *verifiable*, and
> block claims the dossier doesn't back. Highest-leverage phase for a resume tool — it makes the two
> things that matter (did the write land? is the claim true?) **machine-checkable** rather than
> prompt-hoped. Closes gap **#4**; realizes ADR-006 (structured/verified tool channel) and ADR-007
> (separate fail-closed verifier). See [`../../ROADMAP.md`](../../ROADMAP.md).

**Status:** planned · **Depends on:** P1 (0003), P2 (0004) · **Unblocks:** P4 · **Last updated:** 2026-07-10

---

## Goal

Turn "declared done" into "verified done", and "stated fact" into "grounded fact". A weak model must
not be able to (a) claim an edit it never made, or (b) invent a metric / company / credential that
isn't in the profile.

## Build in slices (highest-risk phase — review each tight)

1. **Structured/verified tool channel** — the foundation everything consumes.
2. **Verified-completion gate** — "saved/logged" claims checked against a real write.
3. **Grounding gate (MVP) + honest outcome taxonomy** — fabricated claims caught in-loop.
4. **Promote to the separate fail-closed Guardian verifier** (ADR-007).

---

## Slice 1 — Structured / verified tool channel (ADR-006)

**Widen `ToolResult`** → `(ok, content, structured, verified)`, with `structured`/`verified`
**defaulting to `None`** so existing `ToolResult(ok, content)` call-sites (permission denials, MCP
routing, reads) keep working — this contains the blast radius.

**Receipts are built coach-side in `tools.py`** — no dossier-service change needed, because every
dossier write already returns a usable receipt: `create_application → {id}`, `update_application →
{…row}`, `save_resume → {version}`, `edit_resume → {content, version}`, `save_profile/edit_profile →
{content, version}`, `create_project → {id, upserted}`, `update_project → {…row}`, `delete_* →
{deleted}`, `add_contact → {contact_id}`. A `_write_result(tool, status, body)` helper maps each write
to `structured = {op, id/external_id/version/…}` extracted from that body, and sets `verified`.

**No-laundering rule:** a write that returns `2xx` but **no usable receipt** (empty body, no
id/version) → `ok=False`, `verified=False`. A `4xx/5xx` stays the existing teaching error. This is the
core value: an unconfirmed write is no longer a silent success.

**Review harness — verified already-correct (no change built):** `orchestrator.py:189` already returns
`status="error"` / `"no structured review produced"` for a no-submit repo, so it is **not** laundered
as success; the batch surfaces those per-repo errors in its content. Confirmed against current code —
no change was needed.

**As built (slice 1):** contained to `tools.py` — `ToolResult(ok, content, structured, verified)` +
`_write_result`. Passed a focused adversarial review (clean). One nuance for **slice 2**: a no-op
`update_application`/`update_project` returns the row, so `verified=True` even though nothing changed —
the verified-completion gate must distinguish a real change from a no-op (use `op`/`changed_fields`,
not just presence of a receipt).

**Deferred (documented, not built here):** the ADR-006 *normalize-history* pass. The loop already
appends a tool result for **every** `tool_call_id` (sequential, parallel, duplicate-skip, and
permission-deny paths) and the P1 synthesis turn already `_flatten_for_synthesis`, so there is no
orphaned-toolUse case today. Revisit only if that invariant ever breaks.

## Slice 2 — Verified-completion gate

A **per-turn mutation ledger** in `loop.py` records each successful write's `structured` receipt. When
the model calls `finish_answer` with a claim of having *saved / logged / updated* something, cross-check
it against the ledger; if unbacked, append a **challenge** and loop instead of finishing.

**As built (slice 2):** the gate fires only when a write **verb + a dossier noun** co-occur (so advisory
replies like "I updated my recommendation" don't false-trigger), and is suppressed when a write rides in
the **same batch** as the finish (a `batch_has_write` pre-scan). `review_repos` now returns a **verified
receipt** when it files projects, so a legitimate "reviewed and saved N projects" is not challenged — a
focused adversarial review caught this as **ship-blocking** (review_repos went through `_format`, so the
gate was guaranteed to false-challenge the flagship bulk path *and* the challenge text told the model to
`save_project`, which the prompt forbids after `review_repos`). Bounded by `COMPLETION_CHALLENGE_CAP=2`.
**Known limitations (for slice 3):** a plain-reply over-claim ("I saved your resume" with **no**
`finish_answer`) is not yet gated — the grounding gate covers final-answer *content*; and a no-op
`update` still counts as a verified write.

### As-found (live smoke test, P3 deploy — reliability fixes ahead of slice 3)

Deploying slices 1–2 and running real turns surfaced failures the transcript alone
hid; the docker logs (infra message-count climb, `review(...): hit max_steps without
submit_review`, dossier read/write mix) were the authoritative record. Fixed before
slice 3, because a loop that **spins to the step budget never reaches a clean final
answer for the grounding gate to check** — these are prerequisites, not scope creep:

- **Read-only spin after a bulk tool (loop.py).** The coach kept issuing *different*
  reads (profile, project searches) after `review_repos` had already filed the
  projects, burning the whole budget. Identical-signature loop-detection can't see it
  (each read differs). Added a **convergence guard** (`READ_STREAK_CAP`): after N
  reads with no write and no plan progress, inject a `[converge]` nudge to act or
  finish (bounded by `PROGRESS_NUDGE_CAP`, never a dead loop).
- **Hallucinated tool name (tools.py).** gpt-oss invented `save_application`
  (over-generalizing the `save_*` family) and got a bare `unknown tool: …` with no
  recovery path, feeding the spin. Now a **teaching error** maps the miss
  (`save_application → create_application/update_application`) and lists the real
  catalog. Prompt also names the application tools explicitly.
- **Empty / premature finish (loop.py).** A `finish_answer` with no summary/content
  streamed as a blank "(done)". Now **challenged** (bounded by the same
  `COMPLETION_CHALLENGE_CAP`) so the model produces a real summary or acts.
- **Redundant `read_profile` (loop.py).** The pinned profile was re-read repeatedly
  despite the prompt ban. A read with no profile edit this turn is **short-circuited**
  to a reminder (and counts toward the convergence guard).
- **Reviewer gives up silently (careeragent-review/subagent.py).** `review_one`
  returned `None` on `max_steps`, dropping the repo. Added a **salvage turn** (one
  forced-`submit_review`-only call, the review-side analog of the coach's synthesis)
  so a repo it actually read still yields a partial review; the salvage runs at
  **≥medium** effort (a low-effort forced submit was seen live still refusing).
- **Review must be THOROUGH, never a punt (prompts.py + review config).** Live, the
  coach *declined* "review my GitHub … folder by folder, file by file" as "too many
  hours" and offered a workaround. Per the user's standing preference (thorough over
  fast), the prompt now forbids declining a review in any phrasing and routes it to
  `review_repos` (a dedicated per-repo reviewer, not a skim) or direct `mcp__github__*`
  file reads for one deep repo; and the reviewer runs a **thorough** pass —
  `REVIEW_EFFORT` low → **medium**, `PER_REPO_MAX_STEPS` 16 → **24**.
- **Config.** Deployed `AGENT_MAX_STEPS` reconciled 20 → 40 (spec default); the
  ceiling is a backstop — the convergence guard is the real anti-spin mechanism.

Tests: `test_loop_hygiene.py` (+6), `test_tools.py` (+1, 1 updated),
`careeragent-review/test_subagent.py` (+2). The honest-outcome taxonomy (below) will
make the spin/uncertain cases *observable* — today the logger shows no per-turn outcome.

## Slice 3 — Grounding gate (MVP) + honest outcome taxonomy

New `src/agent/grounding.py`, run as an `on_stop` guard on the drafted final answer (depth-0 only):
- **Tier 1 (deterministic, no model call):** extract candidate claims (percentages, counts,
  "N stars", team/dollar sizes, dates, employer names, titles, awards, certs, compliance standards)
  and check each against a **dossier oracle** (master profile + applications + projects, and read-only
  GitHub stats via the MCP client). A claim with no backing record is a **phantom** → block and
  re-prompt naming exactly which claim is unsupported, or have the coach say "cannot verify".
- **Honest outcome taxonomy:** derive `{final | max_steps | unverified | ungrounded}` from the settle /
  verify audit; an unconfirmed turn is emitted to `careeragent-logger` as **uncertain**, never
  "success".

**As built (slice 3):** the trigger was a live resume run — the coach, tailoring to a legal-tech job
that wanted TypeScript, **invented a "Legal-Tech Prototype" project and asserted "deep expertise in
TypeScript"**, neither in the profile/projects (verified against the dossier DBs). Persona hardening
stopped invented *numbers* but not invented *skills/projects*, so this is the structural fix.
- **Scope narrowed to the two proven-fabricated categories, for precision over recall:** a
  **vocabulary-bounded** SKILL/TECH check and a DOMAIN-experience check (`grounding.py`), each
  **word-boundary matched** so "Java" isn't satisfied by "JavaScript" and a trailing `.`/`,` doesn't
  defeat `c++`. The wider claim taxonomy (employers, certs, dates, numbers) stays for slice 4's verifier;
  numbers are already `[add metric]` placeholders via the persona.
- **Scope guard:** only **resume-like** drafts are gated (`looks_like_resume` needs ≥2 section headers),
  and that check runs **before** the dossier corpus fetch — so an ordinary chat reply that merely
  *mentions* a technology is never gated and never triggers a dossier round-trip.
- **Wired at all THREE final-answer exits** in `loop.py` (finish_summary, plain-reply accept, synthesis)
  so no path bypasses it. A phantom with budget left → a bounded `[converge]`-style re-prompt
  (`GROUNDING_CHALLENGE_CAP=2`) naming the exact unsupported terms; after the cap it ships but the turn
  is logged **`ungrounded`**. The corpus is fetched **once** (cached) and never raises (guarded).
- **Honest outcome taxonomy delivered:** `run_agent` writes a terminal `outcome` (`final | max_steps |
  unverified | ungrounded | model_error`) into a caller-supplied sink; `api.py` emits it on
  `stream_complete` instead of the hard-coded `"success"` — the gap confirmed twice in live logs (every
  turn, incl. punts, was logged `success`). `GROUNDING_GATE_ENABLED` is a kill-switch (default on).
- Tests: `tests/test_grounding.py` (14 — extractor, oracle, scope guard, loop challenge, taxonomy).
  Deferred: grounding the *synthesis* text (budget-exhausted path just logs `max_steps`), and alias
  coverage (`Postgres`↔`PostgreSQL`) — noted for slice 4.

### As-found (live multi-agent audit, 2026-07-16) — CORRECTED PREMISES

A 26-agent audit of every container log + the logger/dossier DBs, with an adversarial
verification pass, **refuted 5 of 14 claims** we had been planning against. Slice 4 must be
scoped against *this* reality, not the original story:

- **The GC AI job never required TypeScript.** It names no language at all. TypeScript bled in
  from the **Edison Scientific** JD 34 minutes earlier in the same conversation. At 21:40 the
  coach was honest ("Some TypeScript/JavaScript experience from the CareerAgent frontend"); by
  22:07, drafting for GC AI, it had become "Deep expertise in Python, TypeScript." **The real
  failure mode is a requirement bleeding across conversation context and escalating from a hedge
  to an overclaim** — no term-presence gate catches that. This is slice-4 work.
- **"Legal-Tech Prototype" never reached the dossier** — it lived only in chat. What *did*
  persist (application `591a3e9f`) is **"Secure Data Pipelines"** — an invented project built from
  ordinary words, which the skill/domain vocabularies could never see. The original fabrication
  was caught only *by accident* (the word "legal" happened to be in `_DOMAIN_VOCAB`).
- **TypeScript IS in the corpus** via `OpenAgent-os` (`Python 70%, TypeScript 20%`). It looked
  phantom only because of the corpus-starvation bug below. **It is not a clean phantom test case.**
  Open question for slice 4: 20% of one repo backing an unhedged "deep expertise" is an overclaim
  *by weight*, and the gate has **no proportionality logic** — it tests token presence only.
- **The gate had never executed in production.** `grep -c "ground|phantom|challenge"` over the api
  logs → 0; no `ungrounded` outcome has ever existed. Import success is not behavior.

**Fixed in response (all live-verified):**
- **Project-existence check** (`grounding.py`) — extract entry titles from the draft's Projects
  section and verify each against the dossier. Previously an invented *"Quantum Trading Engine —
  led a team of 40 at Goldman Sachs"* shipped `grounded=True`. Strict phrase containment is
  deliberate: token-overlap scoring calls "Secure Data Pipelines" backed because *secure*, *data*
  and *pipelines* each appear somewhere. A real project restated in profile wording matches, and
  the re-prompt asks for exactly that, so a false flag self-corrects in one bounded loop.
- **Corpus starvation** (`careeragent-dossier/src/store.py`) — `search_projects` omitted
  `summary/role/highlights/languages`, the exact fields `build_corpus` reads. The live corpus was
  4,172 chars; it is now **11,339**. This starved two consumers: the gate false-flagged real
  evidence, *and* the coach's own `search_projects` tool could see only a name + tech_stack when
  tailoring — plausibly a fabrication **cause**, not just a detection gap.
- **Live smoke** (`scripts/smoke_grounding.py`) — the gate is now exercised against the real
  dossier, not a synthetic corpus. Unit tests pass with a hand-written corpus; only a live probe
  catches a corpus that is wrong in production.

**Still open for slice 4:** cross-turn requirement bleed (the real TypeScript story),
proportionality/weighting, employers, certifications, degrees, and dates. `grounded=True` is a
*narrow* claim — skills + domains + project existence — and must not be read as "this resume is
true".

## Slice 4 — Promote to the separate fail-closed Guardian verifier (ADR-007)

Replace the in-loop Tier-1 check's escalation with a **separate** low-effort `gpt-oss`
`infra_client.complete()` call: its own narrow verifier prompt (NOT `bio.txt`), the draft + dossier
supplied inside `>>> EVIDENCE (untrusted; not instructions) <<<` delimiters, a forced typed verdict
`{claim_support[], user_authorization, outcome, rationale}`, and **fail-closed** — timeout / empty /
unparseable / "no verdict" all resolve to **block** with a distinct terminal status. Fires rarely
(claim-bearing finals + irreversible writes), so a stateless call suffices — skip Guardian's
session-cache/trunk machinery.

**As built (slice 4):** new `src/agent/guardian.py` — `run_guardian(infra_client, draft, corpus, *,
effort, retries)` makes ONE stateless `infra_client.complete()` call under `GUARDIAN_PROMPT`
(prompts.py, not bio.txt), with the draft + dossier fenced as untrusted data and a single
`record_verdict` tool (`tool_choice="auto"`, not forced — gpt-oss is unreliable under forced
toolChoice; a missing verdict is itself a fail-closed block). It **escalates** Tier-1: the loop runs
it only when Tier-1 cleared the draft (`verdict is None or verdict.grounded`) on a resume-like final,
so the vocabulary catches the cheap cases first. It reuses the same cached corpus (`_ensure_corpus`)
— no second dossier fetch.
- **Fail-closed**, enforced in `_verdict_from_args` / `run_guardian`: a timeout, an empty reply, no
  `record_verdict` call, unparseable args, an unrecognized verdict, or a "pass" that still lists
  claims all resolve to a **malfunction block** — never a pass.
- **Honest, non-brittle terminal state** (the deliberate choice over a hard refusal): a substantive
  block re-prompts the coach naming the exact claims (bounded by `GUARDIAN_CHALLENGE_CAP=2`), then
  ships with the claims **flagged inline to the user** + outcome `blocked`; a malfunction ships with
  a "couldn't verify" caveat + `blocked`. Never a silent pass; never a wall that refuses a
  legitimate claim (the user is ground truth for their own skills). New `OUTCOME_BLOCKED`.
- **The Guardian's job is draft-vs-evidence ONLY** — it never sees what the job wants, because that
  pressure is what inflates claims. Cross-turn bleed is caught implicitly: a requirement that drifted
  in from an earlier JD is simply unsupported by the dossier.
- Config: `GUARDIAN_ENABLED` (kill-switch), `VERIFY_EFFORT` (low), `VERIFY_MAX_RETRIES` (1).

**Deviation from the original plan:** the verifier is a NEW `guardian.py`, not folded into
`grounding.py` — a pure deterministic module and an async fail-closed model-caller are different
concerns. The verdict schema is `{verdict, unsupported_claims[], rationale}` (not the sketched
`{claim_support[], user_authorization, …}`) — user-authorization of irreversible writes is a P4
concern, out of scope here.

---

## Files touched (by slice)

**Slice 1:** `src/agent/tools.py` (widen `ToolResult`, `_write_result`) · `careeragent-review/src/harness/{subagent.py,orchestrator.py}` · `careeragent-api/src/client/review.py` · tests: `test_tools.py`, `careeragent-review/tests/test_subagent.py`+`test_harness.py`, and touch-ups where the widened `ToolResult` is asserted.
**Slice 2:** `src/agent/loop.py` (ledger + gate) · `src/agent/prompts.py` (challenge) · `tests/test_verified_completion.py` (new).
**Slice 3:** `src/agent/grounding.py` (new) · `src/agent/loop.py` (on_stop hook + taxonomy) · `src/agent/prompts.py` (cannot-verify rule) · `src/client/logger.py` (outcome event) · `src/backend/api.py` (thread outcome) · `.env.example` (`GROUNDING_GATE_ENABLED`) · `tests/test_grounding.py` (new).
**Slice 4:** `src/agent/guardian.py` (new — the separate verifier) · `src/agent/loop.py` (Tier-2 escalation at both ship exits, `OUTCOME_BLOCKED`, `_settle_outcome`, params) · `src/agent/prompts.py` (`GUARDIAN_PROMPT` + challenge strings) · `src/backend/api.py` (Config `GUARDIAN_ENABLED`/`VERIFY_EFFORT`/`VERIFY_MAX_RETRIES` + thread) · `src/client/logger.py` (`blocked` outcome) · `.env.example` · `tests/test_guardian.py` (new).
**No dossier-service change** (receipts already returned). **No new service.**

## Acceptance

- [ ] A dossier write surfaces a `structured` receipt (`{op, id/version…}`); a `2xx`-empty write is `ok=False`/`verified=False`. *(slice 1)*
- [ ] `careeragent-review` "no structured review produced" is `ok=False`, not a laundered success. *(slice 1)*
- [ ] "I updated your resume" is blocked unless the ledger holds a matching write this turn. *(slice 2)*
- [x] An invented skill/domain in a resume draft is caught and re-prompted (or shipped as `ungrounded`). *(slice 3 — vocabulary-bounded MVP; metrics already placeholdered)*
- [x] Each turn emits a typed outcome; a punt/uncertain turn is never logged as "success". *(slice 3)*
- [x] Verifier failure (timeout/empty/unparseable/no-verdict) resolves to **block**, never pass. *(slice 4 — `run_guardian` fail-closed; `tests/test_guardian.py`)*
- [ ] P1/P2 behavior and existing suites stay green throughout.

## Non-goals

- **The interactive channel** (ask_user/approval/resume) — P4 (0006). The gate *blocks and re-prompts
  the model*; it does not (yet) pause to ask the user.
- **Reach / ingestion** — P5 (0007).
- **Guardian's session-cache/trunk machinery** — the verifier fires rarely, so a stateless call
  suffices; skip the cost-amortization infra.

## Design decisions

- **Why build the receipt coach-side, not change dossier?** Every dossier write already returns an
  id/version. Extracting the receipt in `tools.py` keeps Phase 3's slice 1 contained to careeragent-api
  + the review harness (no dossier redeploy), and the coach is the only consumer that needs it.
- **Why `2xx`-empty → `ok=False`?** The whole point of the trust layer: a write the store didn't
  confirm with a receipt must not read as success, or the verified-completion gate has nothing solid to
  check against.
- **Why a separate verifier (ADR-007), and why only in slice 4?** A same-model "grade your own answer"
  hook is the same context that fabricated. But a separate call adds latency/cost, so the deterministic
  Tier-1 dossier check (slice 3) catches the cheap, common cases first; the separate verifier is the
  escalation for the ambiguous ones.
- **Why fail-closed?** Absence of a valid verdict is exactly the "no structured review produced" bug in
  another guise; the trust layer must treat "couldn't verify" as "don't ship it", never as "pass".

---

*careeragent-api — Phase 3 (verified-completion + grounding). Part of the CareerAgent system. Port 8001.*
