# 0001 — careeragent-code: the read-only code workspace

> A new microservice that gives the coach a REAL local checkout of the user's GitHub repos, so a deep
> code review works off actual files instead of the github-MCP's 6 KB-capped, one-file-at-a-time API
> straw (see careeragent-api/specs/0016-deep-code-review.md). On the careeragent-fetch mold: a small,
> stateless-except-for-a-cache box that isolates a blast radius (a GitHub PAT + untrusted repo content)
> away from the coach.

**Status:** shipped (on-demand tools + Slice E `/refresh` nightly warm) · **Depends on:** git + ripgrep in the image; a read-only GitHub PAT · **Port:** 8012

## What it is (and is NOT)

- **IS:** clone-on-demand + pull of `owner/repo` into a cache volume, and READ-ONLY file access over the
  result — grep, read a file, list the tree. Holds the PAT so callers stay credential-less.
- **IS NOT:** a code EXECUTOR (nothing cloned is ever run — ADR-011), a writer (never pushes/edits), or a
  full-account mirror (clone-on-demand, LRU-capped cache).

```
careeragent-api (no PAT)
     │  http://careeragent-code:8012  (X-API-Key)
     ▼
careeragent-code  — HOLDS the read-only PAT; clones --depth 1 into /cache; greps/reads files
     │
     ▼
github.com  (clone/pull over HTTPS with the PAT; read-only)
```

## Endpoints (X-API-Key: CODE_API_KEY, except /health)

| Method | Path | Purpose |
|---|---|---|
| POST | `/sync` | `{repo:"owner/repo"}` → shallow-clone or `pull` into the cache; returns `{repo, head_sha, files, bytes, cached}`. Idempotent (skips if head unchanged). |
| POST | `/grep` | `{repo, pattern, [glob], [max]}` → ripgrep over that repo; returns bounded `{matches:[{path,line,text}], truncated}`. |
| GET | `/file` | `?repo=&path=` → one file's text, size-capped; path-traversal-guarded. |
| GET | `/tree` | `?repo=` → the file tree (dirs/files, sizes), bounded. |
| GET | `/list` | cached repos + their head sha + last-synced. |
| POST | `/refresh` | **(Slice E)** `{limit?}` → DISCOVER the user's owner repos + clone/pull each into the cache, newest-pushed first, BOUNDED + fail-soft. Returns `{discovered, refreshed, skipped, errors, repos, bytes}`. The nightly warm; separate from on-demand `/sync`, which is untouched. |
| GET | `/health` | `{status, service}` — no auth. |

## Safety (the whole reason it's its own box)

- **PAT isolation** — the read-only `GITHUB_PAT` lives ONLY here (like the github-MCP caddy proxy). It is
  used to clone/pull over HTTPS AND — since Slice E — to DISCOVER the user's owner repos via one GitHub REST
  call (`GET /user/repos`, the token in an `Authorization` header). It is never returned in any response and
  never appears in a log or an error message (discovery errors carry only a status code). Widened from
  clone/pull-only; still isolated in-box, still read-only.
- **No execution** — the service only runs `git` and `rg` (fixed argv, never a shell); it never runs
  anything FROM a cloned repo (no build, no hooks — clone with `core.hooksPath=/dev/null`, no submodule
  auto-init). ADR-011.
- **Path-traversal guard** — `/file` and `/grep` resolve every path INSIDE the repo's cache dir and reject
  anything that escapes (abs paths, `..`, symlinks out). `repo` must match `^[\w.-]+/[\w.-]+$`.
- **Caps** — clone `--depth 1`, size + file-count ceilings per repo (skip/trim a monster repo), an LRU
  eviction cap on total cache size, and a per-`git`/`rg` timeout so one bad repo can't wedge the box. The
  Slice E `/refresh` sweep is additionally BOUNDED by a repo-count cap (`CODE_REFRESH_MAX_REPOS`) and a byte
  budget (`CODE_REFRESH_BUDGET_BYTES`, default a fraction of the cache cap) so a full sweep can never evict a
  repo it warmed earlier in the same pass; each discovered name is re-validated by `require_repo` before any
  clone (the GitHub payload is untrusted), and a per-repo failure is counted and skipped (the sweep continues).
- **Content is DATA** — file content returned here is fenced as untrusted by careeragent-api (a repo can
  carry adversarial strings); this box makes no trust claim about it.

## Wiring

- **careeragent-code (NEW):** `src/{backend/api.py, gitops.py, workspace.py, search.py, safety.py,
  schemas.py, security.py}`; `docker/code/Dockerfile` (python + git + ripgrep); `docker-compose.yml`
  (service + `code-cache` volume, on `careeragent-network`); `.env(.example)` (`CODE_API_KEY`, read-only
  `GITHUB_PAT`, cache caps); docs + tests.
- **careeragent-api:** `client/code.py` (CodeClient) + the `sync_repo`/`code_search`/`read_code`/
  `list_repo_tree` read tools + the `deep-code-review`/`code-content-ideas` skills + the reviewer subagent
  toolset. See 0016.
- **careeragent-jobs (Slice E):** the nightly `repo_presync` job kind calls this box's `/refresh` (its own
  `client/code.py` CodeClient, carrying only `CODE_API_KEY` — never the PAT). See careeragent-jobs/specs/0002.

## Acceptance

- [ ] `/sync` clones `--depth 1` into the cache and is idempotent on an unchanged head; a second sync pulls.
- [ ] `/grep`, `/file`, `/tree` return real content, bounded, and REJECT a path that escapes the repo dir.
- [ ] The PAT never appears in a response; no cloned code is ever executed (no hooks, no build).
- [ ] A monster/hostile repo hits a size/time cap instead of wedging the box.
- [ ] `/refresh` discovers the user's owner repos and warms them BOUNDED (count + byte budget), fail-soft per
      repo, and a discovery failure returns a clean non-2xx (never a 500 crash); the PAT never leaks in the error.

*careeragent-code — the read-only code workspace. Part of the CareerAgent system. Port 8012.*
