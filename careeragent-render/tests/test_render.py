"""
tests/test_render.py — the rendering substance (pure functions, no API, no network).

Covers: a known résumé renders to a real PDF (`%PDF-` magic) and a real DOCX
(`PK\\x03\\x04` zip magic); empty/whitespace résumé and bad format raise
ValueError; the markdown subset parses correctly (headings, bullets, hr,
paragraphs, **bold**/*italic*); and — the important one — résumé text containing
reportlab's markup metacharacters `<`, `>`, `&` renders WITHOUT raising.
"""
import pytest

import render

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"  # DOCX is a zip

SAMPLE = """\
# Ada Lovelace
Analytical Engine Programmer | ada@example.com | (555) 010-1234

## Summary
Pioneering **computer scientist** with *deep* experience in algorithm design.

## Experience
### Lead Analyst — Analytical Engine Co.
- Designed the first published algorithm for a machine.
- Collaborated with Charles Babbage on the Analytical Engine.

## Skills
- Mathematics
- Algorithm design

---
References available on request.
"""


# ---------------------------------------------------------------------------
# real bytes: PDF + DOCX magic
# ---------------------------------------------------------------------------
def test_render_pdf_has_pdf_magic():
    data = render.render(SAMPLE, "pdf", title="Ada Lovelace")
    assert isinstance(data, bytes)
    assert len(data) > 500
    assert data.startswith(PDF_MAGIC), data[:16]


def test_render_docx_has_zip_magic():
    data = render.render(SAMPLE, "docx", title="Ada Lovelace")
    assert isinstance(data, bytes)
    assert len(data) > 500
    assert data.startswith(ZIP_MAGIC), data[:16]


def test_format_is_case_insensitive():
    assert render.render(SAMPLE, "PDF").startswith(PDF_MAGIC)
    assert render.render(SAMPLE, "DocX").startswith(ZIP_MAGIC)


# ---------------------------------------------------------------------------
# bad input -> ValueError (the API maps these to 400)
# ---------------------------------------------------------------------------
def test_empty_resume_raises_valueerror():
    for bad in ("", "   ", "\n\t  \n"):
        with pytest.raises(ValueError):
            render.render(bad, "pdf")


def test_bad_format_raises_valueerror():
    with pytest.raises(ValueError):
        render.render(SAMPLE, "rtf")
    with pytest.raises(ValueError):
        render.render(SAMPLE, "")


# ---------------------------------------------------------------------------
# the escaping test: reportlab Paragraph treats text as mini-HTML, so raw
# `<`, `>`, `&` must be escaped or layout breaks. This must NOT raise.
# ---------------------------------------------------------------------------
def test_markup_metacharacters_render_without_raising():
    tricky = (
        "# R&D Engineer <Systems>\n\n"
        "## Experience\n"
        "- Built C++ & <legacy> pipelines handling >1M events/sec at <5ms.\n"
        "- Reduced cost by >30% while <2 incidents/yr (A&B teams).\n\n"
        "Contact: a<b>c & d>e\n"
    )
    pdf = render.render(tricky, "pdf")
    assert pdf.startswith(PDF_MAGIC)
    docx = render.render(tricky, "docx")
    assert docx.startswith(ZIP_MAGIC)


def test_escape_pdf_helper():
    assert render._escape_pdf("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    # `&` is escaped first so the `&` we introduce for `<`/`>` isn't doubled.
    assert render._escape_pdf("<") == "&lt;"
    assert render._escape_pdf("&") == "&amp;"


# ---------------------------------------------------------------------------
# markdown subset: block parsing
# ---------------------------------------------------------------------------
def test_parse_blocks_headings_bullets_hr_paragraphs():
    blocks = render._parse_blocks(SAMPLE)
    kinds = [k for k, _ in blocks]
    assert ("h1", "Ada Lovelace") in blocks
    assert ("h2", "Summary") in blocks
    assert ("h3", "Lead Analyst — Analytical Engine Co.") in blocks
    assert "bullet" in kinds
    assert "hr" in kinds
    assert "para" in kinds


def test_hr_vs_bullet_disambiguation():
    # `---` is a rule; `- x` is a bullet.
    assert render._parse_blocks("---")[0][0] == "hr"
    assert render._parse_blocks("- x")[0] == ("bullet", "x")
    # 3+ dashes only -> hr; fewer or with text -> not hr
    assert render._parse_blocks("----")[0][0] == "hr"


def test_wrapped_paragraph_lines_join():
    blocks = render._parse_blocks("line one\nline two\n\nnext para")
    paras = [t for k, t in blocks if k == "para"]
    assert paras == ["line one line two", "next para"]


def test_crlf_newlines_are_handled():
    blocks = render._parse_blocks("# Title\r\n\r\n- a\r\n- b\r\n")
    assert ("h1", "Title") in blocks
    assert ("bullet", "a") in blocks
    assert ("bullet", "b") in blocks


# ---------------------------------------------------------------------------
# markdown subset: inline bold / italic
# ---------------------------------------------------------------------------
def test_parse_inline_bold_and_italic():
    spans = render._parse_inline("a **bold** and *italic* end")
    assert render.InlineSpan("a ", False, False) in spans
    assert render.InlineSpan("bold", True, False) in spans
    assert render.InlineSpan("italic", False, True) in spans


def test_parse_inline_plain_text_single_span():
    spans = render._parse_inline("just plain text")
    assert spans == [render.InlineSpan("just plain text", False, False)]


def test_inline_to_pdf_markup_wraps_and_escapes():
    # `**C++ & <x>**` -> escaped, then wrapped in <b>…</b>
    out = render._inline_to_pdf_markup("**C++ & <x>**")
    assert out == "<b>C++ &amp; &lt;x&gt;</b>"


# ---------------------------------------------------------------------------
# filename suggestion
# ---------------------------------------------------------------------------
def test_filename_from_title_and_default():
    assert render._filename("pdf", "Ada Lovelace") == "ada-lovelace.pdf"
    assert render._filename("docx", None) == "resume.docx"
    assert render._filename("pdf", "   ") == "resume.pdf"


# ---------------------------------------------------------------------------
# regressions from the P7 #16b adversarial review
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ctrl", ["\x00", "\x08", "\x0b", "\x0c", "\x1b", "\x07"])
def test_control_chars_render_both_formats_without_raising(ctrl):
    # An identical résumé with a stray control char must render as BOTH pdf and
    # docx (docx/lxml used to raise on these while pdf rendered fine).
    resume = f"# Ada{ctrl} Lovelace\n\n## Skills\n\n- Python{ctrl} and Rust\n"
    for fmt, magic in (("pdf", PDF_MAGIC), ("docx", ZIP_MAGIC)):
        data = render.render(resume, fmt, title="Ada")
        assert data.startswith(magic)
    # sanitizing (which render() applies before layout) drops the control char
    assert ctrl not in render._sanitize_text(resume)


def test_sanitize_strips_control_chars_keeps_normal_text():
    assert render._sanitize_text("a\x00b\x1bc") == "abc"
    assert render._sanitize_text("Python & C++ <legacy>") == "Python & C++ <legacy>"  # normal text intact
    assert render._sanitize_text("tab\tkept") == "tab\tkept"


def test_oversized_paragraph_is_split_into_bounded_blocks():
    # A single ~150 KB one-line paragraph would make reportlab O(n^2) (tens of
    # seconds) — it must be split into many bounded blocks so layout stays cheap.
    huge = "word " * 30000                      # ~150 KB, one paragraph, no blank lines
    blocks = render._parse_blocks(huge)
    assert len(blocks) > 50                      # split, not one monster block
    assert all(len(t) <= render._MAX_BLOCK_CHARS for _, t in blocks)


def test_bounded_render_is_fast_and_valid():
    import time
    huge = "achievement " * 16000               # ~192 KB single paragraph
    t0 = time.perf_counter()
    data = render.render(huge, "pdf")
    assert data.startswith(PDF_MAGIC)
    assert time.perf_counter() - t0 < 10.0       # was tens of seconds unbounded


def test_single_monster_token_is_hard_split():
    # A single word longer than the cap is hard-split (no whitespace to break on).
    pieces = render._chunk("x" * 5000, render._MAX_BLOCK_CHARS)
    assert len(pieces) >= 3 and all(len(p) <= render._MAX_BLOCK_CHARS for p in pieces)
    assert "".join(pieces) == "x" * 5000
