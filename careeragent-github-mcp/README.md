# careeragent-github-mcp

> **The GitHub MCP boundary for CareerAgent** — GitHub's official `github-mcp-server`, run
> **read-only** over streamable HTTP, holding the GitHub credential so `careeragent-api` never
> has to. It's the *only* service that sees the PAT.

---

## Overview

CareerAgent's coach reviews the user's actual GitHub repositories — to turn real projects into
evidence in the [`careeragent-dossier`](../careeragent-dossier) projects library — via the
**Model Context Protocol (MCP)**. This service is that link: it runs GitHub's **official**
[`github-mcp-server`](https://github.com/github/github-mcp-server) in streamable-HTTP mode and
exposes its repo-review tools on the shared `careeragent-network`.

Its whole reason to exist as a separate service is **credential isolation**:

```text
careeragent-api (the agent, NO PAT)  ──HTTP──▶  careeragent-github-mcp (holds the PAT)  ──▶  GitHub
```

The GitHub Personal Access Token lives **only** in this service's `.env`. `careeragent-api`
connects to it by service name and never holds the token — the same compartmentalized-secret
discipline every other boundary in the system follows (infra / logger / memory / dossier each
own their own secret).

It runs the **official image unchanged** — no custom code, just configuration.

---

## Safety: read-only, three ways

1. **`--read-only`** — a server-side flag; the server refuses every write operation regardless of
   the token.
2. **A read-only PAT** — Contents + Metadata *Read-only* (see `.env.example`).
3. **The agent** independently filters write tools out of the catalog and gates non-read `mcp__*`
   tools as mutating.

So the coach can *read* your repos but can **never** modify them.

---

## Where this fits

```text
                       ┌───────────────────────────────────────────────┐
   careeragent-api ──▶ │  careeragent-github-mcp                        │
   (mcp__github__*)    │  github-mcp-server  http --read-only :8082     │
                       │  toolsets: repos, users                        │
                       │  GITHUB_PERSONAL_ACCESS_TOKEN = <your PAT>      │
                       └───────────────────────┬───────────────────────┘
                                               │  GitHub REST/GraphQL (read-only)
                                               ▼
                                          github.com  (your repos)
```

`careeragent-api` reaches it at `http://careeragent-github-mcp:8082/mcp` on the shared network.

---

## Setup

```bash
docker network create careeragent-network        # once, shared by all services (if not already)

cp .env.example .env
# paste a READ-ONLY fine-grained PAT into .env (see .env.example for the exact scopes)

docker compose up -d
docker logs careeragent-github-mcp                # "HTTP server listening addr=0.0.0.0:8082"
```

Then point `careeragent-api` at it (in `careeragent-api/.env`), PAT-less:

```env
GITHUB_MCP_URL=http://careeragent-github-mcp:8082/mcp
```

Restart `careeragent-api`; its log shows `GitHub MCP ENABLED (tools=N, …)` and the coach gains
`mcp__github__*` tools.

---

## The PAT

Fine-grained token (GitHub → Settings → Developer settings → Fine-grained tokens):

- **Repository access:** *All repositories* (your public **and** private repos) — or *Public
  repositories* to exclude private.
- **Repository permissions:** **Contents → Read-only**, **Metadata → Read-only**. Nothing else.

Public repos you contributed to but don't own are readable anyway (public data).

---

## Configuration

| Variable | Where | Purpose |
|---|---|---|
| `GITHUB_PAT` | `careeragent-github-mcp/.env` | The read-only PAT the server uses to reach GitHub. Lives **only** here. |
| `GITHUB_MCP_URL` | `careeragent-api/.env` | `http://careeragent-github-mcp:8082/mcp` — how the agent reaches this service (PAT-less). |

Toolsets / read-only are set on the `command:` in `docker-compose.yml` (`--read-only`,
`--toolsets context,repos,users`). Add toolsets there if the coach needs more (e.g. `issues`).

---

## Design decisions

- **Why a separate service, not the api holding the PAT?** Credential isolation. GitHub is an
  external third party and the token can reach private repos; keeping it in its own boundary means
  a compromise of the api never exposes it, and the token rotates independently. Matches how every
  other boundary owns its own secret.
- **Why the official image, unchanged?** It *is* the integration — GitHub maintains the tools
  (repos, code search, users, …). We add only configuration (read-only, toolsets) and the network
  boundary. Less to maintain, always current.
- **Why streamable HTTP, not stdio?** Our world is HTTP microservices on a shared network; a
  networked MCP endpoint fits it, where stdio (a local subprocess pipe) does not.

---

## License

The official `github-mcp-server` image is GitHub's, under its own license. This wrapper
(compose + docs) is Apache 2.0 © 2026 William McKeon. See [LICENSE](LICENSE).

---

*careeragent-github-mcp — part of the CareerAgent system. Internal port 8082.*
