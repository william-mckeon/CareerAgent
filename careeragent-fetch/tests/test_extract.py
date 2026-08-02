"""
tests/test_extract.py — file-safety + extraction. Hermetic: fixtures are built
in-test (a real minimal PDF and a real DOCX); no external files, no network.
"""
import io

import pytest

import extract


# ---------------------------------------------------------------------------
# in-test fixtures
# ---------------------------------------------------------------------------
def _build_pdf(text: str | None) -> bytes:
    """A minimal, valid single-page PDF with a correct xref table.

    text=None → an empty content stream (a page with no extractable text, i.e.
    the "scanned/image-only" shape).
    """
    stream = b"BT /F1 24 Tf 72 700 Td (" + (text or "").encode("latin-1") + b") Tj ET" \
        if text else b" "
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objs) + 1
    out += b"xref\n0 " + str(n).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (
        b"trailer\n<< /Size " + str(n).encode() + b" /Root 1 0 R >>\n"
        b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF"
    )
    return bytes(out)


def _build_docx(text: str) -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# magic-byte validation (never trust the extension)
# ---------------------------------------------------------------------------
def test_plain_text_renamed_is_rejected_415():
    # A plain-text blob (as if uploaded as resume.pdf) has no magic → 415.
    with pytest.raises(extract.UnsupportedFile) as e:
        extract.extract_document(b"just some text, not really a pdf or docx")
    assert e.value.status_code == 415


def test_legacy_doc_is_rejected_415():
    data = extract._OLE_MAGIC + b"\x00" * 32
    with pytest.raises(extract.UnsupportedFile) as e:
        extract.extract_document(data)
    assert e.value.status_code == 415


def test_zip_without_document_xml_is_rejected_415():
    # A valid zip that is NOT a docx (no word/document.xml).
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "not a docx")
    with pytest.raises(extract.UnsupportedFile) as e:
        extract.extract_document(buf.getvalue())
    assert e.value.status_code == 415


def test_empty_upload_is_rejected_415():
    with pytest.raises(extract.UnsupportedFile):
        extract.extract_document(b"")


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def test_valid_pdf_extracts_text():
    data = _build_pdf("Hello Resume")
    result = extract.extract_document(data, max_pdf_pages=30, max_text_chars=1000)
    assert result.format == "pdf"
    assert "Hello" in result.text
    assert result.chars == len(result.text)
    assert result.truncated is False


def test_scanned_pdf_returns_422():
    data = _build_pdf(None)   # a page with no text
    with pytest.raises(extract.NoTextExtracted) as e:
        extract.extract_document(data, max_pdf_pages=30, max_text_chars=1000)
    assert e.value.status_code == 422
    assert "scanned" in e.value.detail


def test_corrupt_pdf_returns_400(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("broken xref")

    monkeypatch.setattr(extract.pdfplumber, "open", boom)
    with pytest.raises(extract.CorruptFile) as e:
        extract.extract_document(b"%PDF-1.4 broken", max_pdf_pages=30, max_text_chars=100)
    assert e.value.status_code == 400


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def test_valid_docx_extracts_text():
    data = _build_docx("Hello DOCX resume line")
    result = extract.extract_document(data, max_pdf_pages=30, max_text_chars=1000)
    assert result.format == "docx"
    assert "Hello DOCX" in result.text


def test_text_is_capped_and_marked_truncated():
    data = _build_docx("x" * 500)
    result = extract.extract_document(data, max_pdf_pages=30, max_text_chars=100)
    assert result.truncated is True
    assert result.chars == 100
