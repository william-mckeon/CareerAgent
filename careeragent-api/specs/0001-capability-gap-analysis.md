# 0001 — Capability-gap analysis (the coach vs. real agents)

> The **source of truth** for the capability roadmap. It records what the CareerAgent coach can
> do today, grounded in `src/agent/loop.py`; the twenty capabilities it lacks that four mature
> coding agents all have; and which repo is the best template for each. Every phase spec
> (0003–0009) traces back to a gap here. See [`../../ROADMAP.md`](../../ROADMAP.md) for the
> phase grouping and build order.

**Status:** reference · **Last updated:** 2026-07-08

---

## Provenance

Four agents were reviewed folder-by-folder, file-by-file, first for three-bug hardening
(fabrication, momentum, verified-completion) and then for pure capability gaps:

- **openagent_code** (Python) — same stack as the coach; the closest copy-paste template.
- **opencode** (SST, TypeScript/Effect) — richest question tool, background subagents, skills-as-slash.
- **Codex** (OpenAI, Rust) — unbounded persist-to-completion loop, request_user_input with auto-resolve, rollout resume.
- **Cline** (TypeScript, VSCode) — sharpest looping mechanism (terminal-tool completion), self-correction scaffolding.

All four converged: the coach is missing the whole **agentic-autonomy class** that these agents
supply as a *harness around the model*, not a feature here or there.

---

## Baseline — what the coach can do today (grounded in `src/agent/loop.py`)

**Tools it exposes** (from `src/agent/tools.py`): `read_profile`, `search_applications`,
`get_application`, `search_projects`, `get_project`, `save_profile`, `edit_profile`,
`create_application`, `update_application`, `delete_application`, `add_contact`, `save_resume`,
`edit_resume`, `save_project`, `update_project`, `delete_project`, `review_repos`, plus
`mcp__github__*` (read-only, only when an MCP client is wired in).

**Loop behavior** (`run_agent`): one `/chat` turn is a bounded `for step in range(max_steps)`
with `DEFAULT_MAX_STEPS = 12` (`loop.py:41,127`). Each step sends `{messages, tools, tool_choice:"auto"}`
to `infra_client.complete` (inner turns non-streaming so full tool_calls are available before acting).

- **Continue:** if the model returned tool_calls, gate each via `permissions.decide`, execute the
  allowed ones, append each result as a `role:"tool"` message, loop.
- **Stop (success):** the **first** step the model returns **no** tool_calls, that prose is accepted
  as the final answer and streamed (`loop.py:155-159`).
- **Stop (punt):** all 12 steps consumed while still tool-calling → logs `hit max_steps` and streams
  the fixed punt "I've taken several steps without wrapping up… could you tell me a bit more about
  what you'd like to do next?" (`loop.py:194-196`).
- **Stop (error):** `infra_client.complete` raises → streams "I couldn't reach the model just now."

**Modes** (`permissions.py`): `plan` (read-only), `default` (writes need approval — but there is no
interactive channel, so mutations are **denied** and the model is told to ask the user to enable
edit mode), `acceptEdits` (writes allowed; destructive deletes still need confirmation), `bypass`
(everything).

**What it already has:** a bounded multi-step tool loop; multiple (sequential) tool calls per step;
permission-gated writes; final-answer + tool-activity streaming; teaching-error self-correction
(`tools.dispatch` never raises); read-only GitHub MCP; one hardwired delegate (`review_repos` →
careeragent-review); whole-profile injection every turn; `reasoning_effort` passthrough.

**What it fundamentally cannot do** (the gaps below): ask a question mid-task and continue · plan /
track todos · delegate general subagents · resume interrupted work · fetch a JD from a URL · ingest
a PDF/DOCX · run skills/slash workflows · exceed the hard 12-step ceiling · execute tools in parallel
· run background/scheduled jobs · render an artifact.

---

## The two headline gaps (the ones the user named)

### Looping / autonomy
**Today:** continuation depends entirely on the weak model volunteering another tool call; the
first plain-text turn ends the run (`loop.py:155-159`), or step 12 punts (`194-196`).
**Real agents never stop on plain text.** Cline is the cleanest template — a run ends **only** when
the model calls a terminal completion tool (`submit_and_exit`); a premature prose turn triggers an
injected "[SYSTEM] this run is not complete — continue working" and the loop continues.
openagent_code, on exhaustion, spends one final **synthesis** turn instead of punting. **Fix:** P1
(spec 0003).

### Asking the user questions
**Today:** `canAskUserQuestions = false` — there is no question tool. The model's only way to "ask"
is plain text, which immediately **ends** the turn (`loop.py:155-159`). So the weak model **guesses**
at forks it should not guess (which role, IC vs lead, one page vs two) and bakes the wrong assumption
into someone's resume. **Real agents** have a first-class question tool that **pauses the loop and
resumes the same run** with the answer as a tool result. **Fix:** P4 (spec 0006) — the
sessions-backed pause/resume channel.

---

## The twenty gaps (by family)

| # | Capability | Family | Priority | Phase | Resume-domain payoff |
|---|---|---|---|---|---|
| 1 | Persist-until-done loop (flip stop rule, soft budget, synthesize on exhaustion) | looping | critical | P1 | actually finishes "tailor all 6 bullets", doesn't abandon at sentence 1 |
| 2 | Ask a clarifying question mid-task, resume same run | ask | critical | P4 | stops guessing the target role and baking it into a resume |
| 3 | Explicit plan / TODO pinned each turn | planning | high | P1 | the weak model can't drop steps of a multi-app tailoring job |
| 4 | Verified-completion / anti-fabrication gate | self-corr | high | P3 | can't claim "I saved your resume" or invent a metric it never wrote |
| 5 | Retry transient failures + validate tool args | self-corr | high | P2 | a Bedrock throttle doesn't dead-end the whole turn |
| 6 | Fetch a JD from its URL + web search | research | high | P5 | paste a link, not the whole posting; mirror company language |
| 7 | Ingest an uploaded PDF/DOCX resume | attachments | high | P5 | the #1 onboarding step — stop making users hand-paste |
| 8 | General subagent delegation + worker→reviewer | delegation | high | P6 | a bullet-critic / JD-researcher / draft-then-critique pass |
| 9 | Resume interrupted multi-step work | resume | high | P4 | a long tailoring job survives a reload / step-budget hit |
| 10 | Loadable skills + slash workflows (`/tailor`, `/ats-check`) | skills | medium | P7 | expert method encoded once, followed consistently |
| 11 | Context compaction for long sessions | resume | medium | P6 | long multi-application sessions survive gpt-oss's window |
| 12 | Interactive per-action permission approval | ask | medium | P4 | "Save this rewrite? / Delete this app?" — makes default mode usable |
| 13 | Parallel tool reads within a step | other | medium | P2 | reading profile+app+projects at once is a latency win |
| 14 | Loop/repeat detection + "you didn't act" nudge | self-corr | medium | P2 | stops the weak model stalling silently |
| 15 | Mid-run steering + clean interrupt | looping | medium | P4 | "actually target the Staff role" mid-run, not next turn |
| 16 | Render resume (PDF/DOCX) + ATS keyword score | other | medium | P7 | turns "edited text in a DB" into a real deliverable |
| 17 | Agent-authored durable memory (preferences) | other | low | P7 | "targets senior PM, metric-first bullets" always in context |
| 18 | Background/async subagents + cron jobs | delegation | low | P7 | weekly role scan, 10-day follow-up reminders |
| 19 | Typed structured streaming progress | streaming | low | P7 | live checklist / tool cards instead of plain reasoning text |
| 20 | Plan-vs-Act propose→confirm→execute handoff | planning | low | P7 | "here's my approach — approve?" before editing |

---

## Per-repo standouts (best template for each capability)

**openagent_code** (closest, same Python loop shape): synthesis-on-exhaustion (`agent.py:198-212`);
verified-completion gate (`ctx.mutations` ledger + `_unverified_items`/`_completion_challenge`);
grounding/factuality gate; `update_plan` pinned every turn; `ask_user`, `spawn_agent` — the most
literal copy-paste targets.

**opencode:** truly unbounded loop (`maxSteps = agent.steps ?? Infinity`) with a graceful wrap-up
only when a cap is set; the richest question tool (multiple-choice + multi-select + free-text with
auto "type your own"); background async subagents with auto-notification ("do not poll"); durable
event-sourced sessions with steer-vs-queue; skills with progressive disclosure auto-exposed as slash
commands.

**Codex:** `run_turn` with no step ceiling driven by `needs_follow_up` + auto-compaction; `request_user_input`
with `autoResolutionMs` (60–240s auto-proceed); `request_permissions` (model escalates its own write
mid-turn); rollout resume (`codex resume --last`) with an interrupted-turn marker; bulk fan-out
(`spawn_agents_on_csv`).

**Cline:** terminal-tool-only completion (`agent-runtime.ts:604-712`) — flipping the coach's stop
rule is "one `if`"; `submit_and_exit(verified)` self-check; `focus_chain` two-way TODO; worker→reviewer
team; self-correction scaffolding (noToolsUsed nudge, repeatedToolCall detection, tooManyMistakes,
per-tool retry); a cron subsystem.

---

## Non-goals (this analysis)

- **Implementation detail** — the *how* lives in each phase spec (0003–0009), not here.
- **The three-bug hardening port plan (v1–v4)** — the grounding gate, structured/verified tool
  channel, ContextSection manager, CoachHooks, and the Guardian verifier are the *mechanisms* that
  several phases adopt; they are folded into 0003/0005/0006, not re-listed here.
- **Coding-agent machinery** — sandbox/exec/filesystem tools, provider catalogs, plugin
  marketplaces, git-checkpoint plumbing, cloud transport, and any train/distill flywheel are out of
  scope for a resume coach on a hosted model.

---

## Design decisions (pointer)

The cross-cutting decisions that constrain every phase — stay on gpt-oss (not Claude), terminal-tool
completion, one pause/resume channel for four capabilities, 7 phases not 20, structured-tool-channel
as the keystone, grounding via a *separate* fail-closed verifier — live in
[`0002-architecture-decisions.md`](0002-architecture-decisions.md).

---

*careeragent-api — capability-gap analysis. Part of the CareerAgent system. Port 8001.*
