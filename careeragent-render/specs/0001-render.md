# 0001 — careeragent-render: the résumé document renderer

> A new microservice that renders a résumé's markdown into downloadable **PDF**
> or **DOCX** bytes — a fast, **model-free**, **stateless** box for the coach.
> One small, **pure** box: no database, no model calls, no network egress, no
> storage. Port **8009**. Phase 7 (#16, artifact generation — the HEAVY half;
> `careeragent-ats` is the light half).

## Goal

The coach (`careeragent-api`) has an edited résumé draft as markdown and the user
wants a file to download. Producing a clean PDF/DOCX from markdown is a
deterministic layout problem — it needs no model, no round-trip, and none of the
user's stored data. The heavier binary-generation dependencies (reportlab,
python-docx) also do not belong inside the coach process.

So it is isolated into a tiny box: `{resume, format, title?}` in, document bytes
out. It holds nothing and reaches nothing. The caller persists the bytes (via
`careeragent-dossier`); this service just renders and returns.

```
careeragent-api ──POST /render {resume, format, title?}──▶ careeragent-render
                ◀── {content_b64, format, bytes, filename} ──
```

## The spine

```
POST /render {resume, format, title?}
  → if resume blank/whitespace           → 400  "resume text is required to render."
  → if format not in {pdf, docx}         → 400  "format must be 'pdf' or 'docx'."
  → if len(resume.utf-8) > MAX_RESUME_BYTES → 413 "resume too large to render."
  → blocks = parse_blocks(resume)        (headings / bullets / paragraphs / rules)
  → bytes  = render(resume, format, title)   (reportlab PDF  |  python-docx DOCX)
              rendered in-memory (BytesIO), off the event loop (asyncio.to_thread)
  → {content_b64: b64(bytes), format, bytes: len(bytes), filename}
```

## Contract

**`POST /render`** (`X-API-Key: RENDER_API_KEY`) → `RenderResponse`
`{content_b64:str, format:str, bytes:int, filename:str}`.
Body `{resume:str, format:"pdf"|"docx", title?:str}`.
- `400` — empty/whitespace `resume` (`"resume text is required to render."`).
- `400` — `format` not `pdf`/`docx` (`"format must be 'pdf' or 'docx'."`).
- `413` — `resume` over `MAX_RESUME_BYTES`, measured on actual UTF-8 bytes
  (`"resume too large to render."`).
- `503` — `RENDER_API_KEY` unset. `401` — bad/missing `X-API-Key`.
- `content_b64` decodes to exactly `bytes` bytes of the file.

**`GET /health`** → `{status, service}` (no auth).

## The markdown subset (`src/render.py`)

A **focused résumé subset**, deliberately not a full markdown engine. Parsed by
pure, deterministic functions.

1. **Block parse (`_parse_blocks`)** — line-oriented; `\r\n`/`\r` normalized:
   - `# ` → **h1** (name / top line), `## ` → **h2** (section), `### ` → **h3**
     (sub-header);
   - `- ` or `* ` → **bullet**;
   - `---` (3+ dashes alone, matched **before** the bullet rule) → **hr**;
   - a blank line flushes the current paragraph;
   - any other line accumulates into a **paragraph** (wrapped lines joined by a
     space).
2. **Inline parse (`_parse_inline`)** — splits a line into `(text, bold, italic)`
   spans on `**bold**` / `*italic*` (bold matched first). Non-nesting; unmatched
   `*` stays literal. No `__bold__` / `_italic_`, no links/images/code.

### Rendering
- **PDF (`_render_pdf`, reportlab.platypus)** — `SimpleDocTemplate` + `Paragraph`
  / `Spacer` / `HRFlowable` with a small style sheet: name (H1) 20pt bold under a
  header rule; section (H2) 12.5pt bold with a thin underline rule; H3 10.5pt
  bold; 10pt body; tight `•` bullets. Base-14 Helvetica (no embedded fonts).
- **DOCX (`_render_docx`, python-docx)** — `Title` / `Heading 1` / `Heading 2`
  styles, `List Bullet` paragraphs, bold/italic runs, and a bottom-border
  paragraph for `---` (OOXML `w:pBdr`, since python-docx has no native rule).
  Reasonable margins.

### Escaping (the sharp edge)
reportlab's `Paragraph` treats its text as a mini-HTML/XML dialect, so a raw `<`,
`>`, or `&` in résumé text would break parsing or drop content. `_escape_pdf`
escapes `&` **first** (so the `&` introduced for `<`/`>` isn't doubled) then
`<`/`>`, per span, on the raw text — the only `<`/`>` in the final markup are the
`<b>`/`<i>` tags the renderer itself adds. Tested with a résumé full of `C++ &
<legacy>` / `>1M` text.

## Behaviour rules
1. **Validate before layout.** Empty résumé and bad format are `400`; an oversize
   blob is `413` **before** any parsing — a giant input can't blow up memory.
2. **In-memory only.** Render to `BytesIO`; never touch the filesystem.
3. **Off the event loop.** The synchronous reportlab/python-docx work runs via
   `asyncio.to_thread` so it can't block `/health` or concurrent requests.
4. **Deterministic layout.** Same résumé → same layout. (reportlab embeds a PDF
   creation date, so PDF *bytes* aren't bit-identical run-to-run; acceptable.)
5. **Degrade, don't fail.** Unsupported markdown becomes plain text rather than
   an error.

## Residual limits (honest)
- **Not a general markdown→PDF engine.** No tables, links, images, code fences,
  blockquotes, nested/ordered lists, inline code. It renders the résumé shape.
- **Base-14 fonts only.** Helvetica family; no custom/embedded fonts, limited
  coverage for exotic scripts. Keeps the image thin and self-contained.
- **No OCR / images.** Text-only.
- **PDF creation date** makes PDF bytes non-reproducible byte-for-byte.
- **In-memory buffering** — bounded by `MAX_RESUME_BYTES`.

## Non-goals
- Fetching/extracting the résumé (that's `careeragent-fetch`; text arrives ready).
- Storing the bytes (the coach owns persistence, via `careeragent-dossier`).
- Any model call, rewriting, or content generation — this box only *lays out*
  text it is given.
- ATS scoring / analysis (that's `careeragent-ats`, the light half of #16).

## Design decisions
- **Separate service, not in the api agent** — keeps heavy binary-generation
  deps (reportlab, Pillow, lxml) and a self-contained, independently testable
  renderer out of the coach, reusable by any future surface.
- **No model** — layout is deterministic; a renderer is faster, free,
  reproducible, and needs none of the user's data.
- **reportlab + python-docx only** — both pure-Python with self-contained
  manylinux wheels (reportlab bundles its fonts + pulls Pillow; python-docx rides
  lxml), so the slim image needs **no apt layer** and no build toolchain
  (confirmed empirically by building the image and rendering inside it).
- **Escape-then-markup** — the one genuinely tricky bit; deliberately unit-tested.

---
*careeragent-render — part of the CareerAgent system. Internal port 8009.*
