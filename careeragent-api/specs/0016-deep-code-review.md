# 0016 — Phase 8 #24: Deep code review (a local code workspace)

> Promoted from the P8 scaffold (0013). The coach's repo review today is a portfolio *summarizer* —
> `careeragent-review` reads a README + a couple of manifest files through the github-MCP (capped at
> **6,000 chars** of file content, ~12 steps/repo) and files a project CARD (name/summary/tech_stack/…).
> That is right for "turn my repos into résumé projects"; it is structurally incapable of a **line-level**
> read. Deep code review needs the actual files — full, greppable, with structure — so give the coach a
> local checkout and read-only code tools.

**Status:** scaffolded · **Depends on:** P6 (subagents), careeragent-github-mcp (cheap lookups), the
scheduler #18b (optional freshness) · **Last updated:** 2026-07-23

## Why the current path can only give an overview

- `careeragent-review` output fields are `name, summary, role, tech_stack, highlights, languages,
  repo_url, stars` — a portfolio entry, not an analysis. Its prompt: *"start with the README, then read
  package/manifest/config files."*
- File content reaches the model through the MCP capped at 6,000 chars (`_MAX_RESULT_CHARS`), one API call
  per file, bounded to a handful of files. You cannot review a codebase through a 6 KB straw.

So "look closer at my code → overview" is by design. The fix is not a bigger cap; it is **a real checkout**.

## Shape (ratified) — a read-only code workspace + reviewer subagents

- **New service `careeragent-code` (port 8012 + a `code-cache` volume)** — the "code workspace" box, on
  the careeragent-fetch mold (isolated, single concern). It **clones on demand** (`git clone --depth 1`)
  and **pulls** the user's repos into a cache volume, and exposes READ-ONLY file tools over them:
  `sync`, `grep` (ripgrep), `file`, `tree`, `list`. Holds a **read-only GitHub PAT**, isolated exactly
  like the github-MCP caddy proxy — the coach stays PAT-less. **No code is ever executed** (ADR-011);
  cloned code is data to read, never to run. Clone-on-demand + LRU-capped cache, NOT a full-account mirror.
- **The coach gets read tools** (careeragent-api) that relay to it: `sync_repo`, `code_search`,
  `read_code`, `list_repo_tree`. Code content is fenced as untrusted DATA (a repo can contain adversarial
  strings) — mined for review, never obeyed.
- **Deep analysis runs in a per-repo reviewer SUBAGENT (reuses P6).** A `deep-code-review` skill drives:
  `sync_repo` → spawn a read-only `reviewer` subagent armed with the code tools → it works the real files
  (grep the tree, read the hot files, follow the structure) → returns a genuine code-level review
  (architecture, notable implementations, real strengths, honest weaknesses). That review (a) files a
  MUCH richer project entry than the summarizer, and (b) is real evidence the résumé + Phase-8 job-matcher
  can draw on.
- **`careeragent-review` stays the cheap portfolio-card path.** Two tools, different jobs: `review_repos`
  for "fill the projects library fast"; the workspace for "go deep on THIS repo." The github-MCP stays for
  cheap lookups (list repos, one file, head sha).

## Companion capability — code-grounded content ideas (#24b)

The driving use case: *"review my repos project by project, compare to what I've posted on X, and give me
X-post ideas."* A `code-content-ideas` skill composes the deep review with the user's OWN posts: the user
PASTES/exports their recent X posts (own-data — X's API is restricted; no scraping), the coach deep-reviews
the repos, diffs "what the code actually does" against "what you've already posted", and proposes grounded
post ideas — each pointing at real code, inventing nothing. On-brand developer personal-branding, grounded.

## How it strengthens the mission (not a pivot to a coding agent)

Deep code → **richer, evidence-backed project entries** → stronger résumés + a sharper Phase-8 job-matcher
(it scores against your ACTUAL work) → plus code-backed content. It bounds strictly to READ/review/portfolio/
content — never editing or running the user's repos. It makes the core loop better, not wider.

## Non-goals

- **Executing** cloned code, or writing to the user's repos. Read-only, no-exec (ADR-011).
- **Mirroring the whole account.** Clone-on-demand + a capped cache.
- **Replacing `careeragent-review` or the github-MCP.** Complementary; the workspace is the deep path only.
- **Scraping X.** Post history is the user's own paste/export.

## Acceptance

- [ ] `sync_repo(owner/repo)` shallow-clones/pulls into the cache; `code_search`/`read_code`/`list_repo_tree`
      return real file content/matches/structure (bounded, path-traversal-safe, PAT-isolated).
- [ ] The coach, via `deep-code-review`, produces a line-level review of a repo (not a card) and files a
      richer project — grounded, nothing invented.
- [ ] `code-content-ideas` proposes X-post ideas each tied to REAL code, compared against the user's pasted
      posts, inventing nothing.
- [ ] No path executes cloned code; the PAT never leaves careeragent-code; the coach stays PAT-less.

*careeragent-api — Phase 8 #24 (deep code review). Part of the CareerAgent system. Ports 8001 (api), 8012 (code).*
