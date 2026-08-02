# careeragent-render — Datasheet

> Precise contract reference. The README is the narrative; this is the contract.

## Quick Reference

| Item | Value |
|---|---|
| Role | Résumé document renderer (markdown → PDF/DOCX bytes) |
| Port / path | `8009` — internal only, no host port |
| Kind | FastAPI; no DB; **no model calls**; **no network egress**; pure-Python renderers |
| Inbound | `POST /render` (X-API-Key: RENDER_API_KEY), `GET /health` |
| Sole client | `careeragent-api` (the coach's résumé-render tool) |
| Outbound | **None.** No sibling calls, no internet. |
| Holds secrets | `RENDER_API_KEY` only. Holds **none** of the user's data (renders and returns; stores nothing). |

## API reference

### `POST /render`
Body (`RenderRequest`): `{ "resume": "<markdown str>", "format": "pdf"|"docx", "title": "<optional str>" }`.

Success `200` (`RenderResponse`):
```json
{ "content_b64": "JVBERi0xLjQ…", "format": "pdf",
  "bytes": 2192, "filename": "jane-doe.pdf" }
```
- `content_b64` — base64 of the raw file bytes. `base64.b64decode(content_b64)`
  recovers the exact file.
- `format` — the format actually rendered: `pdf` | `docx` (lowercased).
- `bytes` — `int`, length of the DECODED content; equals
  `len(base64.b64decode(content_b64))`.
- `filename` — suggested download name, slugified from `title` (else
  `resume.<ext>`), e.g. `resume.pdf`, `jane-doe.docx`.

| Condition | Status | Body |
|---|---|---|
| success | `200` | `{content_b64, format, bytes, filename}` |
| `resume` empty or whitespace-only | `400` | `{"detail": "resume text is required to render."}` |
| `format` not `pdf`/`docx` | `400` | `{"detail": "format must be 'pdf' or 'docx'."}` |
| `resume` over `MAX_RESUME_BYTES` (actual UTF-8 bytes) | `413` | `{"detail": "resume too large to render."}` |
| `RENDER_API_KEY` unset | `503` | — |
| bad/missing `X-API-Key` | `401` | — |

`format` is accepted case-insensitively (`PDF`, `DocX`) and echoed lowercased.

### `GET /health` (no auth)
`{"status": "ok", "service": "careeragent-render"}`

## The supported markdown subset (`src/render.py`)

A focused résumé subset — **not** a general markdown engine. Everything is parsed
by pure, deterministic functions (unit-testable without the API).

| Syntax | Block | PDF (reportlab) | DOCX (python-docx) |
|---|---|---|---|
| `# text` | h1 (name) | 20pt bold + header rule | `Title` style |
| `## text` | h2 (section) | 12.5pt bold + thin rule | `Heading 1` |
| `### text` | h3 (sub-header) | 10.5pt bold | `Heading 2` |
| `- text` / `* text` | bullet | `•` bullet, tight leading | `List Bullet` |
| blank line | paragraph break | — | — |
| `---` (3+ dashes alone) | rule | `HRFlowable` | bottom-border paragraph |
| `**bold**` / `*italic*` | inline | `<b>`/`<i>` markup | bold/italic runs |
| anything else | paragraph | 10pt body | normal paragraph |

**Escaping:** reportlab's `Paragraph` parses its text as a mini-HTML dialect, so
raw `&`, `<`, `>` are escaped (`&`→`&amp;` first, then `<`/`>`) before the `<b>`/
`<i>` tags are added — user text can never inject markup or break layout.
Wrapped lines within one paragraph are joined with a single space. `\r\n` and
`\r` newlines are normalized.

Rendering is in-memory (`BytesIO`, never a temp file). The public entry point is
`render(resume_text, fmt, title) -> bytes`, which raises `ValueError` on empty
résumé / unsupported format (the API maps that to `400`).

## Ownership

### Owns
| Domain | Artifact |
|---|---|
| Markdown-subset parsing (blocks + inline emphasis) | `src/render.py::_parse_blocks`, `_parse_inline` |
| PDF layout | `src/render.py::_render_pdf` (reportlab) |
| DOCX layout | `src/render.py::_render_docx` (python-docx) |
| Markup escaping (`<`,`>`,`&`) | `src/render.py::_escape_pdf` |
| The input size cap (actual bytes) | `backend.api.render_endpoint` |
| Inbound auth | `src/security.py` |

### Does NOT own
| Concern | Owner |
|---|---|
| The résumé text / when to render | `careeragent-api` (the coach's tool) |
| Where the rendered bytes are stored | `careeragent-dossier` (the coach writes them) |
| Fetching / extracting the résumé | `careeragent-fetch` |
| The model | not involved — this service makes no model calls |

## Residual limits (honest)

| Limit | Status |
|---|---|
| **Not a general markdown engine.** No tables, links, images, code fences, blockquotes, nested/ordered lists, `__bold__`/`_italic_`, inline code. | By design — a focused résumé renderer; unknown syntax degrades to plain text. |
| **Base-14 fonts only.** Uses reportlab's built-in Helvetica family; no custom/embedded fonts, no full Unicode coverage for exotic scripts. | Accepted for P7 — keeps the image thin and self-contained. |
| **No OCR / images.** Text-only résumé layout. | By design. |
| **PDF creation date.** reportlab embeds a creation timestamp, so PDF bytes are not bit-identical run-to-run (the *layout* is deterministic). | Accepted (noted in the spec). |
| **In-memory buffering.** The résumé + rendered doc are held in RAM. | Bounded by `MAX_RESUME_BYTES` (413 before layout). |

## Container / deployment
- `python:3.11-slim`; **no apt layer** — reportlab (bundles its Base-14 fonts;
  pulls Pillow), python-docx (via lxml), and the FastAPI stack all ship
  self-contained manylinux wheels (confirmed empirically: a clean `pip install`
  in the slim image resolves wheels only, and `import reportlab, docx` + a real
  PDF/DOCX render succeed inside the container). Non-root uid 1000;
  `PYTHONPATH=/app/src`; `uvicorn backend.api:app` on `:8009`.
- Compose: single service on the external `careeragent-network`; no host port;
  stdlib `/health` healthcheck.

## Cross-references
- `specs/0001-render.md` — design, the markdown subset, honest limits
- `careeragent-api` — the render client + coach tool (to be written)
- `careeragent-dossier` — where the coach stores the rendered bytes

---
*careeragent-render — part of the CareerAgent system. Internal port 8009.*
