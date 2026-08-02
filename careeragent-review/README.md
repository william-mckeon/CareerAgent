# careeragent-review

> **The repo-review harness for CareerAgent.** Reviews the user's GitHub
> repositories — one bounded subagent per repo, fanned out in parallel — and
> files each as a structured project in the evidence library. Port **8007**.

---

## Why it exists

A résumé is built from evidence, and the richest evidence is the user's actual
code. But having the coach (`careeragent-api`) read many repos in one turn
overflows its context and burns its step budget. This service is the fix, using
the pattern [OpenCode](../../OpenCode) proved:

> **The harness partitions the work in code; the model only synthesizes.**

One repo = one bounded child agent with its *own* model context. The main agent
calls this whole thing as a **single tool** (`review_repos`) and gets back a tidy
count — never the raw files.

```
careeragent-api ──POST /review-batch──▶ careeragent-review
                                          │  (partition in code, one child/repo)
                        ┌─────────────────┼─────────────────┐   asyncio.gather
                        ▼                 ▼                 ▼   (parallel, bounded)
                   subagent(repo A)  subagent(repo B)  subagent(repo C)
                        │  each: fresh /complete context + GitHub MCP read tools
                        ▼
                   submit_review (structured JSON)  ──▶  careeragent-dossier /projects
```

## How one repo is reviewed

1. **Idempotency** — get the repo's HEAD sha (GitHub MCP) and compare to the
   `commit_sha` dossier stored last time; **skip** if unchanged (unless `force`).
2. **Bounded subagent** — a fresh `careeragent-infra /complete` tool-loop
   (capped at `PER_REPO_MAX_STEPS`) reads the README + key files via the GitHub
   MCP, then calls the terminal `submit_review` tool with the structured fields.
3. **Write** — upsert into dossier's projects library by `external_id=owner/repo`
   (refresh, not duplicate), stamped with `commit_sha` + `last_reviewed_at`.

Read-only three ways: the GitHub server runs `--read-only`, the PAT is
read-only, and the MCP client filters out write tools.

## API

`POST /review-batch` (`X-API-Key: REVIEW_API_KEY`)
```json
{ "repos": ["owner/repo", "..."],   // optional; else enumerated via the MCP
  "limit": 12, "focus": "backend", "force": false }
```
→ `{ "reviewed": N, "skipped": N, "errors": N, "outcomes": [ {repo, status, detail?, project_id?, commit_sha?} ] }`

`GET /health` → `{status, infra, github_mcp, dossier}` (no auth).

## Setup

```bash
docker network create careeragent-network        # once, shared by all services
cp .env.example .env                              # fill in the keys (see below)
docker compose up -d --build
docker logs careeragent-review                    # "Harness ready ... careeragent-review ready."
```

Then wire `careeragent-api` (its `.env`): `REVIEW_URL=http://careeragent-review:8007`,
`REVIEW_API_KEY=…`, and restart it — the coach gains the `review_repos` tool.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `REVIEW_API_KEY` | inbound auth (only caller: careeragent-api) |
| `INFRA_URL` / `INFRA_API_KEY` | the model gateway (`/complete`) |
| `GITHUB_MCP_URL` | `http://careeragent-github-mcp:8082/mcp` (PAT-less) |
| `DOSSIER_URL` / `DOSSIER_API_KEY` | the projects write target |
| `MAX_REPOS` | cap repos per request (12) |
| `REVIEW_CONCURRENCY` | parallel subagents (4) |
| `PER_REPO_MAX_STEPS` | tool-loop cap per repo (12) |
| `REVIEW_MODEL` | infra route: `base` (default) or `nervous_system` |
| `REVIEW_EFFORT` | `low` \| `medium` \| `high` |

## Cost note

Reviews run on the `base` route (gpt-oss today) at `low` effort — the cheap,
harness-bounded path. The bounding (per-repo step cap, one repo per context)
does far more for quality/cost than a bigger model would; if you ever want the
prose sharper, point `REVIEW_MODEL` at a Claude route in `careeragent-infra`.

## Tests

`pytest` (hermetic — fake infra/MCP/dossier, no network): the subagent tool-loop
(`test_subagent.py`), the orchestrator partition/idempotency/fan-out
(`test_harness.py`), and inbound auth (`test_api.py`).

---
*careeragent-review — part of the CareerAgent system. Internal port 8007.*
