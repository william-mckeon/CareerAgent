# 0001 — careeragent-review: the repo-review harness

> A new microservice that reviews the user's GitHub repositories with an
> **OpenCode-style map-reduce harness** — the harness partitions in code (one
> child per repo), fans out a **bounded per-repo subagent** (each its own
> `careeragent-infra /complete` context reading the repo via the GitHub MCP),
> and upserts each **structured result** into `careeragent-dossier`'s projects
> library. Idempotent per repo+commit. Port **8007**.

## Goal

The coach needs the user's *whole body of work* in the projects library, but
having the main agent read many repos serially in one turn overflows its context
and burns its step budget (observed: it re-read one README 5×). The fix is the
OpenCode insight: **the harness does the decomposition, the model only
synthesizes small per-unit summaries.** Here the unit is one repo.

## The spine (from OpenCode, adapted)

```
POST /review-batch {repos?, limit?, focus?, force?}
  → partition IN CODE: one unit per repo (explicit list, or enumerated via MCP)
  → cap at MAX_REPOS; de-dup
  → asyncio.gather under Semaphore(REVIEW_CONCURRENCY)   [PARALLEL, unlike OpenCode]
      per repo:
        idempotency: HEAD sha (MCP) vs stored commit_sha (dossier) → skip if equal
        subagent: fresh /complete context, GitHub MCP read tools + submit_review,
                  bounded by PER_REPO_MAX_STEPS → structured JSON
        write: POST dossier /projects (upsert by external_id=owner/repo)
  → reduce: {reviewed, skipped, errors, outcomes[]}
```

Four deltas from OpenCode: reads via **GitHub MCP** (not local FS); fan-out is
**parallel** (independent HTTP round-trips); output is **structured JSON** (via a
terminal `submit_review` tool, since `/complete` has no JSON mode); results are
**persisted** to dossier (keyed by repo+commit for skip-if-unchanged).

## Contract

**`POST /review-batch`** (`X-API-Key: REVIEW_API_KEY`) → `ReviewBatchResponse`
`{reviewed, skipped, errors, outcomes:[{repo, status, detail?, project_id?, commit_sha?}]}`.
Body: `{repos?: ["owner/repo"], limit?, focus?, force?}`.

**`GET /health`** → `{status, infra, github_mcp, dossier}`.

## Structured output — the `submit_review` tool

`/complete` has no JSON mode, so the per-repo subagent "returns" its answer by
calling a terminal `submit_review` function tool whose parameters map 1:1 onto
dossier project columns (`name, summary, role, tech_stack, highlights,
languages, repo_url, stars`). The harness reads the tool-call arguments,
allowlists them, and upserts. `name`+`summary` required; the harness fills
`source='github'`, `external_id`, `commit_sha`, `last_reviewed_at`.

## Outbound boundaries (secrets in `.env`, over `careeragent-network`)
- `careeragent-infra` `/complete` — the model (route `REVIEW_MODEL`, default `base`).
- `careeragent-github-mcp` `:8082/mcp` — read-only repo access, **PAT-less**.
- `careeragent-dossier` `/projects` — the write target.

## Bounding (env knobs)
`MAX_REPOS` (12), `REVIEW_CONCURRENCY` (4), `PER_REPO_MAX_STEPS` (12),
`REVIEW_MODEL` (`base`), `REVIEW_EFFORT` (`low`), plus the MCP client's per-call
timeout (60s) and 6000-char result cap.

## Idempotency
Best-effort: get the repo HEAD sha (MCP `list_commits`, perPage=1) and compare
to the `commit_sha` dossier stored last review; skip if equal (unless `force`).
If the sha can't be determined, it **fails open** (reviews). Requires dossier's
`commit_sha` column + `GET /projects?external_id=` (migration 0003).

## Behaviour rules
1. **Fail-soft per repo** — one repo erroring (rate limit, huge repo, model
   error) becomes an `error` outcome; the batch continues.
2. **Read-only, three ways** — server `--read-only`, read-only PAT, client
   write-tool filter. Review never writes to GitHub.
3. **`exclude_unset` on the dossier write** — only fields the reviewer produced
   are sent, so a partial re-review never blanks a populated row.

## Non-goals
- Reviewing repos the user doesn't own/can't read (bounded by the PAT scope in
  careeragent-github-mcp).
- Cross-repo synthesis / ranking (the coach does that when tailoring, reading
  the projects library).
- A UI (the coach triggers this via its `review_repos` tool).

## Design decisions
- **Separate service, not in the api agent** — centralizes the fan-out,
  parallelism, bounding, and idempotency behind one HTTP contract; keeps the
  agent's context tiny (it calls `review_repos` as ONE tool).
- **Writes to dossier directly** — idempotency logic lives with the reviewer; a
  deliberate 2nd producer into the projects table (dossier's DATASHEET notes it).
- **`submit_review` tool, not prose** — the only reliable structured-output path
  through `/complete`.

---
*careeragent-review — part of the CareerAgent system. Internal port 8007.*
