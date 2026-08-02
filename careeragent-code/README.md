# careeragent-code — the read-only code workspace

Gives the CareerAgent coach a **real local checkout** of the user's GitHub repos so a deep code review
works off actual files, instead of the github-MCP's 6 KB-capped, one-file-at-a-time API. Part of P8 #24
(see `../careeragent-api/specs/0016-deep-code-review.md` and `specs/0001-code-workspace.md`).

- **Clone-on-demand** (`git clone --depth 1`) into a cache volume; refresh by fetch+reset.
- **Read-only tools:** `/sync`, `/grep` (ripgrep), `/file`, `/tree`, `/list`.
- **Holds the read-only GitHub PAT** so `careeragent-api` stays credential-less.
- **Never executes cloned code** — no git hooks, no build, symlinks written as plain files; path-traversal
  guarded; per-file/tree/grep/cache caps + timeouts. Runs unprivileged.

## Run

```bash
docker network create careeragent-network      # once, shared by all services
cp .env.example .env                            # set CODE_API_KEY + a read-only GITHUB_PAT
docker compose up -d --build                    # → http://127.0.0.1:8012
```

## Endpoints (X-API-Key: CODE_API_KEY, except /health)

| Method | Path | Body / query | Returns |
|---|---|---|---|
| POST | `/sync` | `{repo:"owner/repo"}` | `{repo, head_sha, files, bytes, cached}` |
| POST | `/grep` | `{repo, pattern, glob?}` | `{repo, matches:[{path,line,text}], truncated}` |
| GET | `/file` | `?repo=&path=` | `{repo, path, content, bytes, truncated}` |
| GET | `/tree` | `?repo=` | `{repo, entries:[{path,bytes}], truncated}` |
| GET | `/list` | — | `[{repo, head_sha, last_used}]` |
| GET | `/health` | — | `{status, service}` |

## Tests

```bash
pip install -r requirements-dev.txt
pytest        # hermetic — git/ripgrep are mocked
```
