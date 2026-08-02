# careeragent-render

> **The résumé document renderer for CareerAgent.** Given a résumé's markdown
> text and a target format, it returns the rendered **PDF** or **DOCX** bytes
> (base64-encoded). A pure, **stateless**, **model-free** box: NO model, NO
> database, NO network egress. Holds **none** of the user's data — it renders and
> returns; the coach persists the bytes. Port **8009**.

---

## Why it exists

The coach (`careeragent-api`) has an edited résumé draft (markdown) and the user
wants a downloadable file. Turning markdown into a clean PDF/DOCX is a
**deterministic layout problem** — it needs no model, no round-trip, and none of
the user's stored data. Binary generation (and its heavier dependencies) also
does not belong inside the coach process.

So it lives in its own tiny box: résumé markdown in, document bytes out. No
model, no DB, no egress, no storage (the caller already has the text and owns
persistence via `careeragent-dossier`).

```
careeragent-api ──POST /render──▶ careeragent-render
                 {resume, format, title?}   (pure rendering; reportlab / python-docx)
                ◀── {content_b64, format, bytes, filename}
```

## What it does

### `POST /render` — résumé markdown in, document bytes out
1. **Validate** — reject an empty/whitespace résumé (`400`), an unsupported
   format (`400`), or an oversize blob (`413`, on actual UTF-8 bytes) before any
   layout work.
2. **Parse a focused résumé-markdown subset** (see below) into blocks —
   headings, bullets, paragraphs, rules — with inline `**bold**` / `*italic*`.
3. **Lay it out** — a clean, professional résumé:
   - **PDF** via **reportlab** (`platypus`): name (H1) large with a header rule,
     bold section headers (H2) underscored by a thin rule, tight bullets.
   - **DOCX** via **python-docx**: Title / Heading styles, `List Bullet`
     paragraphs, bold/italic runs, a bottom-border rule.
4. **Return** the bytes, base64-encoded, with a suggested filename.

Rendering is done in-memory (`BytesIO`, never a temp file) and off the event loop
(`asyncio.to_thread`) so the synchronous layout work can't block `/health` or
concurrent requests.

## The markdown subset (honest scope)

This is a **focused résumé renderer, not a general markdown→PDF engine.** It
understands exactly:

| Markdown | Rendered as |
|---|---|
| `# Heading` | H1 — the name / top line (largest), with a header rule |
| `## Heading` | H2 — a section header (bold, thin underline rule) |
| `### Heading` | H3 — a sub-header (a role / company line) |
| `- item` or `* item` | a bullet list item |
| `**bold**` | **bold** inline |
| `*italic*` | *italic* inline |
| blank line | paragraph break |
| `---` (3+ dashes alone) | a horizontal rule |
| anything else | a normal paragraph |

Not supported (degrade to plain text): tables, links `[..](..)`, images, code
fences, blockquotes, nested lists, ordered lists, `__bold__` / `_italic_`, inline
`` `code` ``. Résumé text containing reportlab's markup metacharacters (`<`, `>`,
`&`) is **escaped** and renders safely.

## API

`POST /render` (`X-API-Key: RENDER_API_KEY`)
```json
{ "resume": "# Jane Doe\n\n## Skills\n- Python\n- Docker\n",
  "format": "pdf", "title": "Jane Doe" }
```
→
```json
{ "content_b64": "JVBERi0xLjQ…", "format": "pdf",
  "bytes": 2192, "filename": "jane-doe.pdf" }
```
`content_b64` decodes to exactly `bytes` bytes of the file. `format` ∈ `pdf | docx`.
`filename` is slugified from `title` (else `resume.<ext>`).

| Condition | Status | Body |
|---|---|---|
| success | `200` | `{content_b64, format, bytes, filename}` |
| empty / whitespace `resume` | `400` | `{"detail": "resume text is required to render."}` |
| `format` not `pdf`/`docx` | `400` | `{"detail": "format must be 'pdf' or 'docx'."}` |
| `resume` over `MAX_RESUME_BYTES` | `413` | `{"detail": "resume too large to render."}` |
| `RENDER_API_KEY` unset | `503` | — |
| bad / missing `X-API-Key` | `401` | — |

`GET /health` → `{ "status": "ok", "service": "careeragent-render" }` (no auth).

## Setup

```bash
docker network create careeragent-network        # once, shared by all services
cp .env.example .env                              # set RENDER_API_KEY
docker compose up -d --build
docker logs careeragent-render                    # "careeragent-render ready."
```

Then wire `careeragent-api` (its `.env`): `RENDER_URL=http://careeragent-render:8009`
and `RENDER_API_KEY=…`, and restart it — the coach gains the résumé-render tool.

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `RENDER_API_KEY` | — | inbound auth (only caller: careeragent-api) |
| `MAX_RESUME_BYTES` | `200000` | input size cap (actual UTF-8 bytes) → `413` |
| `LOG_LEVEL` | `INFO` | log verbosity |

## How the rendering works (and its honest limits)

The parser + renderers live in `src/render.py` and are **pure and
deterministic** — the same résumé always lays out the same way (reportlab embeds
a PDF creation date, so PDF bytes are not bit-identical run-to-run; the layout
is). The one public entry point is `render(resume_text, fmt, title) -> bytes`.

This is a **résumé-shaped renderer, not a full markdown engine** (see the subset
table). It does not do OCR, images, tables, or web fonts; it uses reportlab's
built-in Base-14 fonts (Helvetica family) for a clean, portable look. See
`docs/DATASHEET.md` and `specs/0001-render.md` for the full method and limits.

The container runs unprivileged (uid 1000). No host port is published. The image
needs **no apt layer** — reportlab (which bundles its fonts and pulls Pillow) and
python-docx (via lxml) all ship self-contained manylinux wheels.

## Tests

`pytest` (hermetic — no network, no server):
- `test_render.py` — a known résumé renders to a real PDF (`%PDF-`) and DOCX
  (`PK\x03\x04` zip); empty/bad-format raise `ValueError`; the markdown subset
  parses correctly; and `<`, `>`, `&` render without raising (the escaping test).
- `test_api.py` — inbound auth (401/503), the `400`/`400`/`413` validation, the
  response shape (base64 decodes back to `bytes`), and the unauthenticated
  `/health`.

---
*careeragent-render — part of the CareerAgent system. Internal port 8009.*
