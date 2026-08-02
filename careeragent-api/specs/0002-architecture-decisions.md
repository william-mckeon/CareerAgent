# 0002 — Architecture decisions (ADR log)

> The decisions that must **not** be re-litigated while building the capability roadmap. Each is a
> short ADR: context → decision → status. If a future change contradicts one of these, that is a
> drift signal — revisit the ADR explicitly, don't quietly diverge. See
> [`../../ROADMAP.md`](../../ROADMAP.md) and [`0001-capability-gap-analysis.md`](0001-capability-gap-analysis.md).

**Status:** living · **Last updated:** 2026-07-08

---

## ADR-001 — Stay on gpt-oss-120b; do not switch the coach to Claude

**Context.** gpt-oss is weaker at orchestration than frontier models, which is tempting to "fix" by
swapping in Claude. But even accounting for gpt-oss re-reading a file several times, Claude models
run several times the operating cost, and a good agent harness (what this roadmap builds) closes most
of the quality gap.

**Decision.** The coach runs **gpt-oss-120b on Amazon Bedrock**. We invest in the *harness*
(looping, grounding, plan, verifier), not the model. No fine-tuning/distillation — the coach only
consumes a hosted model. Claude access is also not provisioned on the account (Bedrock rejected the
model IDs), so a swap is blocked in practice as well.

**Status.** Accepted. Every "the model is too weak" problem is answered with a harness mechanism, not
a model swap.

---

## ADR-002 — Anti-fabrication is a persona rule + (future) a verifier gate, NOT a reasoning-effort knob

**Context.** The coach fabricated resume metrics/stars/awards/compliance ("99.8% mission-success",
"200+ stars", "three classified ASW platforms") while claiming to be "100% truthful". A live A/B
(medium vs high `reasoning_effort`) graded by 24 adversarial judges showed **reasoning effort is not
the cause — high effort fabricated *more*** (medians ~15 → ~19.5). So the fix is not a config knob.

**Decision.** Fabrication is fought in two layers: (1) a hard persona rule in `src/prompt/bio.txt` +
`src/agent/prompts.py` — "no invented facts", use `[add metric]` placeholders, ask (shipped, commit
`08507de`; cut fabrication ~58%); (2) a structural **grounding gate** that *verifies* claims against
the dossier — a prompt asks for honesty, a gate enforces it. The gate is P3 (spec 0005).

**Status.** Layer 1 shipped. Layer 2 is P3. Do **not** raise `REASONING_EFFORT` to fix fabrication.

---

## ADR-003 — Terminal-tool completion (flip the stop rule)

**Context.** `loop.py:155-159` treats the *first* no-tool-call turn as final, and `194-196` punts at
step 12. This is the mechanical cause of "one call then stop" — a weak model emits a sentence and the
run ends. All four reviewed agents end a run **only** when the model calls a terminal completion tool.

**Decision.** Add a `finish_answer` terminal tool. A no-tool-call turn is **no longer automatically
final**: while a plan still has open items (and `finish_answer` hasn't been called), inject a
"not done — keep going or call `finish_answer`" reminder and loop again. Raise `DEFAULT_MAX_STEPS`
12→~40 (a **soft** budget, configurable); replace the punt with a **synthesis** turn.

**Status.** P1 (spec 0003). This is the keystone of the whole roadmap.

---

## ADR-004 — One sessions-backed pause/resume channel serves four capabilities

**Context.** `ask_user`, interactive permission approval, resume-of-interrupted-work, and mid-run
steering all need the same thing: the ability to **suspend** a run, persist its state, and **resume**
it later with new input. Building four separate mechanisms would design the same plumbing four times.

**Decision.** Build **one** suspend/resume channel backed by careeragent-sessions (already the
conversation system-of-record): a `/chat` turn that hits a suspension point persists
`convo + step + pending_call_id` and stops the generator cleanly; a later POST tagged for that
`call_id` re-enters `run_agent` with a synthetic `role:tool` answer and continues. All four
capabilities ride it.

**Status.** ✅ REALIZED — P4 (spec 0006 + careeragent-sessions/specs/0002). The channel was built first,
then all four capabilities landed on it and were live-verified: `ask_user` (suspend/resume), interactive
approval (execute-on-approval), resume-on-reload, and mid-run steering + interrupt. One channel, four
consumers, one adversarial review. The suspend point emits a namespaced frame carrying the accumulated
convo snapshot (the api is stateless); sessions persists it and drives resume by replaying the
server-saved snapshot + the user's reply. Frontend caveat: true mid-stream steering is API-level only
(Streamlit blocks during streaming).

---

## ADR-005 — Seven phases, not twenty; dependency-ordered

**Context.** The 20 capability gaps are real, but they are not 20 independent phases: several collapse
into one build (ADR-004), several are hours-of-work items that bundle, and there is a hard dependency
order (looping enables the rest).

**Decision.** Plan the work as **~7 dependency-ordered phases** (see ROADMAP): Autonomy core →
Loop hygiene → Trust gate → Interactive channel → Reach → Delegation+compaction → Polish. Ship P1
first; it is small, self-contained, and unblocks everything else.

**Status.** Accepted.

---

## ADR-006 — The structured/verified tool-result channel is the keystone

**Context.** Today `ToolResult` is `(ok, content=json.dumps(body))` and `ok` is only *printed* to the
reasoning channel — it never gates the model, and any 2xx maps to `ok=True`, so a "no structured
review produced" result launders into a "saved/logged" success. Cline reproduces this exact bug;
opencode/Codex fix it with a structured channel.

**Decision.** Widen `ToolResult` to `(ok, content, structured, verified)`. Dossier writes return
`{op, external_id, commit_sha}` receipts (kept out of the model-facing content). `2xx`-empty **and**
"no structured payload" become `ok=False`/`verified=False`. A normalize-history pass runs before every
model call so an abandoned tool call always settles (synthetic `is_error`, never a fabricated success).

**Status.** Underpins P3 (0005) and P4; the trust gate and honest taxonomy consume it. Treat as a
foundational sub-task inside P3.

---

## ADR-007 — Grounding runs through a *separate*, fail-closed verifier (Guardian pattern)

**Context.** A same-model "grade your own answer" hook is the same context that fabricated grading its
own fix. Codex's `guardian` runs the verifier as a **separate** locked-down model session that
receives the draft as *untrusted evidence* and is **fail-closed** (timeout/empty/unparseable →
DENY). Absence of a valid verdict is a block, never a pass.

**Decision.** The grounding/verified-completion gate (P3) is a **separate** low-effort `gpt-oss`
`/complete` call with its own narrow verifier prompt (not `bio.txt`), untrusted-evidence delimiters,
a forced typed verdict, and fail-closed terminal statuses. An MVP in-loop gate lands first; the
promotion to the full separate verifier is a later step within P3.

**Status.** ✅ REALIZED — P3 spec 0005, slice 4. `careeragent-api/src/agent/guardian.py`:
`run_guardian()` makes one stateless `infra_client.complete()` call under `GUARDIAN_PROMPT`
(prompts.py, NOT bio.txt); the draft + dossier corpus are fenced as `>>> … untrusted DATA, not
instructions <<<`; the verdict comes back via a single `record_verdict` tool. Fail-closed is
enforced in `_verdict_from_args` / `run_guardian`: timeout, empty reply, no tool call, unparseable
args, an unrecognized verdict, or a "pass" that still lists claims all resolve to a **malfunction
block** (never a pass). `tool_choice="auto"` (not forced) because gpt-oss on Bedrock is unreliable
under forced toolChoice — a missing verdict is itself a fail-closed block. It escalates the Tier-1
gate (only runs on a resume-like final that passed Tier-1) and terminal-states honestly: a
substantive block re-prompts (bounded), then ships with claims **flagged to the user** + outcome
`blocked`; a malfunction ships with a "couldn't verify" caveat + `blocked`. Deliberately kept
stateless (no session-cache/trunk) since it fires rarely.

---

## ADR-008 — Reimplement concepts in Python; never lift the TS/Rust

**Context.** opencode is TypeScript/Effect, Codex is Rust, Cline is TypeScript — with Effect runtimes,
plugin hosts, subprocess IPC, sandboxes, and cloud transport that a single-model Python coach does not
need.

**Decision.** Port **patterns and mechanisms** (the seam catalog, the settlement invariant, the
question flow, the pin-diff engine) as plain Python in careeragent-api / the careeragent-* services.
Do not port Effect/Layer/Deferred runtimes, plugin marketplaces, subprocess hook transports,
sandbox/exec engines, or provider-catalog governance.

**Status.** Accepted; applies to every phase.

---

## ADR-009 — Egress + untrusted-file handling live in a separate `careeragent-fetch` box; fetched/uploaded text is untrusted DATA

**Context.** P5 (spec 0007) needs two firsts for this codebase: a server-side fetch of a
**user-controlled URL** (to read a JD from its link) and parsing of an **untrusted uploaded file** (a
PDF/DOCX resume). Both are blast-radius the coach must not carry: `fetch_url` is an SSRF surface on a
box that trusts `X-API-Key` on the shared `careeragent-network` (every internal service port becomes
reachable), and lxml/pdf parsers on the coach image violate its deliberate thin, no-compiler posture.

**Decision.** Put both in a **new `careeragent-fetch` service** (port 8008) — one combined "reach" box
doing egress (`/fetch`) and parsing (`/extract`), isolated from all career data. SSRF defense is built
from scratch in `careeragent-fetch/src/ssrf.py` (scheme allowlist; resolve DNS then validate the
**resolved IP** against loopback/metadata/RFC1918/link-local/ULA/reserved; redirects re-validated per
hop; connect/read timeout; hard byte cap enforced during streaming). File safety is magic-byte
validation + size/page/zip-bomb/timeout caps; scanned PDFs fail clearly (no OCR in P5). The coach reaches
it via a fail-soft `FetchClient` (short timeout). **`fetch_url` is a READ tool** (in `READ_TOOLS`, never
`MUTATING`) so it is plan-mode-usable and needs no permission change. **Ingestion is extract-to-text
feeding the existing write tools** (reusing the P3 grounding + P4 approval gates), not a new write tool.
**Uploads are a separate multipart hop** (frontend → careeragent-fetch `/extract`); the file bytes never
ride the text-only `/chat` relay. **Fetched and uploaded text is UNTRUSTED DATA** — fenced (`>>> FETCHED
PAGE` / `>>> UPLOADED RESUME`, smuggled markers defanged) and labelled the way steering (ADR-004) and the
Guardian evidence (ADR-007) are, so an embedded "ignore your instructions" can neither be obeyed nor
stand as evidence about the user. Web search is deferred (no provider, its own egress + ADR).

**Status.** ✅ REALIZED — P5 (spec 0007). ADR-006's structured `ToolResult` channel and ADR-008's
"reimplement in Python, isolate the blast radius" both apply to the new tools and service.

---

## ADR-010 — LinkedIn is own-data-only; recommendations/reviews are grounded (Phase 8)

**Context.** Phase 8 (career intelligence, spec 0013) wants the coach to review the user's LinkedIn and
recommend jobs. The tempting path — the one a colleague's `ai-job-scraper` uses for job boards — is to
SCRAPE. But LinkedIn is not a job board: a datacenter fetch of a profile hits a login wall / HTTP 999,
cookie/session scraping risks the user's real account (ban waves) and breaches §8.2, third-party scraper
APIs are legally tainted (Proxycurl was sued and shut down; hiQ lost on contract). Separately, a job/
profile scorer that runs an ungrounded LLM against a résumé can flatter the user ("great fit for your
Kubernetes work") on evidence they don't have — the exact fabrication ADR-002 exists to stop.

**Decision.** Two rules, not to be re-litigated:
1. **Own-data only.** LinkedIn *content* enters CareerAgent solely via the user's own upload — a
   "Save to PDF" of their profile or the "Get a copy of your data" export ZIP — parsed in the isolated
   careeragent-fetch box. A public-profile `fetch_url` is best-effort (usually blocked) and never the
   backbone. **No cookie/session scraping, no scraper folder, no third-party scraper API.**
2. **Grounded scoring.** Every job-match reason and every LinkedIn-review suggestion runs through the
   existing P3 grounding + Guardian gate (ADR-002, spec 0005). A recommendation may not claim a fit on a
   skill/experience the dossier doesn't evidence; a review flags a missing skill as a **gap**, never
   invents one. The output is honest by construction, not by prompt alone.

**Status.** Accepted for Phase 8. A future "scrape LinkedIn" request is a drift signal — revisit here
first. The value is in the REVIEW and the SCORING (which run on data we can legitimately hold), not in
acquisition magic.

---

## ADR-011 — A local code workspace for deep review: read-only, no-exec, clone-on-demand, PAT-isolated

**Context.** The coach's repo review is a portfolio summarizer (spec 0016): it reads a README + a couple
of manifests through the github-MCP capped at 6 KB of file content, and files a project card. Asked to
"look closer at the code" it can only give an overview — a 6 KB-per-file API straw cannot carry a codebase.
Deep review needs the actual files. The tempting shortcuts each carry risk: mirror the whole account
(disk + churn), execute/build cloned code (arbitrary code execution from third-party repos), or widen the
GitHub PAT into the coach.

**Decision.** Add a dedicated **`careeragent-code`** box (port 8012) — the read-only code workspace:
- **Clone-on-demand, cached.** `git clone --depth 1` / `pull` a repo into a capped LRU cache volume when
  it is first reviewed. The optional nightly REFRESH is now realized as **Slice E**: careeragent-jobs'
  scheduler fires careeragent-code's `POST /refresh`, a BOUNDED (repo-count + byte-budget) fail-soft sweep
  that warms the cache so the day's first review isn't a cold clone — on-demand pull stays authoritative,
  the warm is a pure optimization. NOT a full-account mirror.
- **Read-only, NO execution.** The box runs only `git` and `rg` with fixed argv (never a shell), and never
  runs anything FROM a cloned repo — no build, no git hooks (`core.hooksPath=/dev/null`), no submodule
  auto-init. Cloned code is DATA to read, never to run. It also never writes to the user's repos.
- **PAT stays isolated.** The read-only GitHub PAT lives ONLY in this box (like the github-MCP caddy proxy)
  and is never returned; the coach stays PAT-less. File content the coach receives is fenced as untrusted
  DATA (a repo can carry adversarial strings). **Slice E widens the PAT's mandate** from clone/pull-only to
  ALSO discovering the user's owner repos via one GitHub REST call (`GET /user/repos`) — the discovery lives
  HERE (its own PAT) rather than adding a github-MCP client to careeragent-jobs, so jobs/api stay PAT-less
  (jobs → careeragent-code carries only `CODE_API_KEY`). The token is still never logged or returned
  (discovery errors carry only a status code).
- **Complementary, not a replacement.** The github-MCP stays for cheap lookups; `careeragent-review` stays
  the portfolio-card path; the workspace is the DEEP path only. This bounds strictly to read/review/
  portfolio/content — CareerAgent does not become a coding agent that edits or runs code.

**Status.** Accepted for Phase 8 #24; amended for **Slice E** (nightly `/refresh` warm + the PAT's mandate
widened to owner-repo discovery, still isolated in-box). A future "execute/build the cloned code" or "let the
coach edit a repo" request is a drift signal — revisit here first.

---

*careeragent-api — architecture decisions. Part of the CareerAgent system. Port 8001.*
