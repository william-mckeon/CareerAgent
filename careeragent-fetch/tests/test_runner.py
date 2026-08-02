"""
tests/test_runner.py — the isolated-extraction boundary (runner.extract_isolated).

Proves the subprocess plumbing round-trips a real result AND propagates typed
ExtractProblems with the correct HTTP status, and that the wall-clock guard fires.
Hermetic: spawns a short-lived child process, no network.
"""
import io

import pytest

import extract
import runner


def _build_docx(text: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_isolated_docx_roundtrips():
    data = _build_docx("Hello isolated resume line")
    result = runner.extract_isolated(data, max_pdf_pages=30, max_text_chars=1000)
    assert result.format == "docx"
    assert "Hello isolated" in result.text
    assert result.chars == len(result.text)


def test_isolated_unsupported_propagates_415():
    with pytest.raises(extract.ExtractProblem) as e:
        runner.extract_isolated(b"not a document at all", max_pdf_pages=30, max_text_chars=100)
    assert e.value.status_code == 415


def test_isolated_empty_docx_propagates_422():
    data = _build_docx("")  # a DOCX with no extractable text
    with pytest.raises(extract.ExtractProblem) as e:
        runner.extract_isolated(data, max_pdf_pages=30, max_text_chars=100)
    assert e.value.status_code == 422


def test_isolated_timeout_raises_504():
    # A 0-second budget forces the wall-clock guard: the child cannot even finish
    # spawning within the deadline, so q.get times out -> ExtractionTimeout. This
    # exercises the same guard the memory-bomb / hang cases rely on.
    data = _build_docx("anything")
    with pytest.raises(runner.ExtractionTimeout) as e:
        runner.extract_isolated(data, max_pdf_pages=30, max_text_chars=100, timeout=0.0)
    assert e.value.status_code == 504
