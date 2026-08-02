# careeragent-review — Datasheet

> Precise contract reference. The README is the narrative; this is the contract.

## Quick Reference

| Item | Value |
|---|---|
| Role | Repo-review harness (fan-out one bounded subagent per repo) |
| Port / path | `8007` — internal only, no host port |
| Kind | FastAPI orchestrator; no DB; httpx clients + MCP SDK |
| Inbound | `POST /review-batch` (X-API-Key: REVIEW_API_KEY), `GET /health` |
| Sole client | `careeragent-api` (the agent's `review_repos` tool) |
| Outbound | `careeragent-infra /complete`, `careeragent-github-mcp :8082/mcp` (PAT-less), `careeragent-dossier /projects` |
| Holds secrets | INFRA_API_KEY, DOSSIER_API_KEY, REVIEW_API_KEY — **no GitHub PAT** |

## Ownership

### Owns
| Domain | Artifact |
|---|---|
| The fan-out / partition / reduce | `src/harness/orchestrator.py` |
| The per-repo bounded tool loop | `src/harness/subagent.py` |
| The reviewer prompt + `submit_review` contract | `src/harness/prompts.py` |
| Idempotency (HEAD sha vs stored commit_sha) | `orchestrator._head_sha` + dossier read |

### Does NOT own
| Concern | Owner |
|---|---|
| The model | `careeragent-infra` (`/complete`) |
| GitHub access + the PAT | `careeragent-github-mcp` |
| The projects table | `careeragent-dossier` |
| When to trigger a review | `careeragent-api` (the coach's `review_repos` tool) |

## API reference

### `POST /review-batch`
Body (`ReviewRequest`): `{repos?: ["owner/repo"], limit?: int, focus?: str, force?: bool}`.
- `repos` omitted → the harness enumerates the authenticated user's repos via the MCP.
- `limit` caps this call (≤ `MAX_REPOS`); `force` re-reviews even if unchanged.

Response (`ReviewBatchResponse`):
```json
{ "reviewed": 2, "skipped": 1, "errors": 0,
  "outcomes": [
    {"repo": "me/a", "status": "reviewed", "project_id": "uuid", "commit_sha": "abc123"},
    {"repo": "me/b", "status": "skipped",  "detail": "unchanged", "commit_sha": "def456"},
    {"repo": "me/c", "status": "error",    "detail": "no structured review produced"}
  ] }
```
`status` ∈ `reviewed | skipped | error`. Auth failure → `401`; `REVIEW_API_KEY`
unset → `503`.

### `GET /health` (no auth)
`{"status": "ok"|"degraded", "infra": "ok"|"unreachable", "github_mcp": "ok"|"unreachable", "dossier": "ok"}`

## The `submit_review` structured-output contract

The per-repo subagent emits its result by calling `submit_review`; the arguments
are allowlisted to the dossier project columns:

| Field | Required | → dossier column |
|---|---|---|
| `name` | ✅ | `name` |
| `summary` | ✅ | `summary` |
| `role` / `tech_stack` / `highlights` / `languages` / `repo_url` / `stars` | — | same |

The harness adds `source='github'`, `external_id='owner/repo'`, `commit_sha`,
`last_reviewed_at`, then `POST`s dossier `/projects` (upsert by `external_id`,
`exclude_unset`).

## Failure modes

| Condition | Behaviour |
|---|---|
| One repo errors (rate limit, huge repo, model error) | `error` outcome; batch continues |
| GitHub MCP down at boot | fail-soft; `/health` degraded; reviews return per-repo errors |
| HEAD sha undeterminable | idempotency fails **open** (reviews) |
| Model never calls `submit_review` within `PER_REPO_MAX_STEPS` | `error`: "no structured review produced" |
| dossier write non-2xx | `error` with the status + body |

## Container / deployment
- `python:3.11-slim`; `uvicorn backend.api:app` on `:8007`; `PYTHONPATH=/app/src`.
- Compose: single service on the external `careeragent-network`; no host port; healthcheck via `/health`.

## Cross-references
- `specs/0001-review.md` — design + rationale
- `careeragent-api/src/client/review.py` + `src/agent/tools.py` — the `review_repos` tool
- `careeragent-dossier` migration `0003_projects_commit_sha.sql` — the idempotency column

---
*careeragent-review — part of the CareerAgent system. Internal port 8007.*
