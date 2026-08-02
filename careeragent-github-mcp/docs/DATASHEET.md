# careeragent-github-mcp — Datasheet

> Contract reference for the GitHub MCP boundary. The README is the narrative; this is the
> precise contract.

---

## Quick Reference

| Item | Value |
|---|---|
| Role | GitHub MCP server (credential-isolated repo review) |
| Kind | GitHub's official `github-mcp-server`, run as-is (no custom code) |
| Image | `ghcr.io/github/github-mcp-server:latest` |
| Transport | Streamable HTTP (MCP) |
| Internal port / path | `8082` / `/mcp` |
| Mode | `http --read-only --toolsets context,repos,users` |
| Sole client | `careeragent-api` (the agent), reached at `http://careeragent-github-mcp:8082/mcp` |
| Holds the PAT | **Yes** — `GITHUB_PERSONAL_ACCESS_TOKEN` (from `GITHUB_PAT` in its `.env`) |
| Exposes to the api | Read-only repo/user tools (`mcp__github__*` after namespacing) |
| Host port | none (internal only) |

---

## Ownership boundaries

### What this service owns
| Domain | Concrete artifact |
|---|---|
| The GitHub credential | `GITHUB_PERSONAL_ACCESS_TOKEN` env (this service, and nowhere else) |
| GitHub API access | the official `github-mcp-server` process |
| Read-only enforcement | `--read-only` server flag |
| Tool scope | `--toolsets context,repos,users` |

### What this service does NOT own
| Concern | Owner |
|---|---|
| Tool selection / the agent loop | `careeragent-api` |
| Turning repos into projects | `careeragent-api` → `careeragent-dossier` |
| The MCP client (namespacing, filtering) | `careeragent-api` (`src/agent/mcp_client.py`) |
| The GitHub tools themselves | GitHub (the official image) |

---

## Contract

- **Inbound:** streamable-HTTP MCP at `http://careeragent-github-mcp:8082/mcp`. `careeragent-api`'s
  MCP client `initialize()`s, `list_tools()`, and `call_tool()` against it. The agent connects
  **PAT-less**; this service authenticates to GitHub with its own env token.
- **Outbound:** GitHub REST/GraphQL (read-only), authenticated with `GITHUB_PERSONAL_ACCESS_TOKEN`.
- **Tools exposed:** the read-only subset of the `repos` + `users` toolsets (e.g. list/search
  repositories, get file contents, list commits/branches, get user). Write tools are not served
  (`--read-only`).

### Auth note
The HTTP server registers OAuth-protected-resource endpoints. Whether it accepts the env token for
PAT-less clients or requires a per-request `Authorization` header is settled at first connect with
a real token; if per-request auth is required, a small auth-injecting proxy is added **inside this
service** so the api stays PAT-less either way.

---

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `GITHUB_PAT` | Yes | Read-only fine-grained PAT (Contents + Metadata: Read-only). Injected as `GITHUB_PERSONAL_ACCESS_TOKEN`. Never leaves this service. |

Server flags (in `docker-compose.yml` `command:`): `--read-only`, `--toolsets context,repos,users`,
`--port 8082`, `--listen-host 0.0.0.0`.

---

## Container / deployment

- **Image:** `ghcr.io/github/github-mcp-server:latest` (distroless; no shell).
- **Compose:** single service on the external `careeragent-network`; no host port, no volume.
- **Restart:** `unless-stopped`.
- **No build:** uses the upstream image directly — no Dockerfile, no source.

---

## Failure modes

| Condition | Behaviour |
|---|---|
| PAT missing / invalid | GitHub calls 401; the agent surfaces the tool error and continues (fail-soft on the api side). |
| This service down | `careeragent-api` logs "GitHub MCP unavailable" at startup and runs with dossier tools only. |
| Write tool attempted | Refused server-side (`--read-only`), and never offered by the agent's filter. |

---

## Cross-references

- `README.md` — setup + design rationale
- `docker-compose.yml` — the exact `command:` and env
- `careeragent-api/src/agent/mcp_client.py` — the MCP client that consumes this service
- [github/github-mcp-server](https://github.com/github/github-mcp-server) — the upstream server

---

*careeragent-github-mcp — part of the CareerAgent system. Internal port 8082.*
