"""
PDF-support feature tests -- run with:  pytest tests/test_phase_pdf_ingestion.py -v

Same two-group split as tests/test_phase2_rag.py:

1. Structural tests (no heavy deps) -- exercise `_load_pdf_document()` /
   `load_and_chunk_documents()` directly against REAL PDFs generated on
   the fly with `reportlab` (not mocked text, not a fake `Chunk` list --
   an actual multi-page PDF and an actual PDF containing a real table,
   matching this project's existing preference for real synthetic test
   data over mocks -- see e.g. tests/test_phase_camera_stream.py).
2. `@pytest.mark.heavy` -- full pipeline through real embeddings + a
   real (temporary) Chroma collection, verifying PDF content is
   retrievable through the EXISTING, unmodified RAG retriever.

Covers spec TEST 1-7:
  TEST 1 -- normal text PDF, full pipeline, retrievable          -> test_pdf_end_to_end_retrieval (heavy)
  TEST 2 -- multi-page PDF, page metadata preserved per chunk    -> test_multi_page_pdf_preserves_page_metadata
  TEST 3 -- PDF with a building-info table, table stays retrievable -> test_pdf_table_content_is_preserved_in_chunks
  TEST 4 -- invalid/corrupted PDF                                -> test_invalid_pdf_is_skipped_not_crashed
  TEST 5 -- PDF with no extractable text                         -> test_blank_pdf_produces_no_chunks_and_warns
  TEST 6 -- RAG query against uploaded PDF content                -> test_pdf_end_to_end_retrieval (heavy)
  TEST 7 -- existing file types still work exactly as before      -> test_existing_md_txt_behavior_unaffected_by_pdf_support
"""

from __future__ import annotations

import logging

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

from app.rag.document_loader import (
    Chunk,
    PDFExtractionError,
    _load_pdf_document,
    load_and_chunk_documents,
    load_raw_documents,
)

pytestmark = []


# ---------------------------------------------------------------------
# Helpers: build REAL PDFs with reportlab (no mocking)
# ---------------------------------------------------------------------


def _make_text_pdf(path, pages_text: list[str]) -> None:
    """A plain multi-page PDF, one reportlab page break per string in `pages_text`."""
    from reportlab.platypus import PageBreak

    doc = SimpleDocTemplate(str(path), pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    for i, text in enumerate(pages_text):
        for para in text.split("\n\n"):
            story.append(Paragraph(para.replace("\n", "<br/>"), styles["Normal"]))
            story.append(Spacer(1, 12))
        if i < len(pages_text) - 1:
            story.append(PageBreak())
    doc.build(story)


def _make_table_pdf(path) -> None:
    """A PDF containing an actual reportlab Table with building info, like the spec's worked example."""
    doc = SimpleDocTemplate(str(path), pagesize=letter)
    styles = getSampleStyleSheet()
    data = [
        ["Building", "Floors", "Height"],
        ["AB1", "7", "45 m"],
        ["AB2", "5", "32 m"],
    ]
    table = Table(data)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    story = [
        Paragraph("VIT Chennai Campus Building Details", styles["Title"]),
        Spacer(1, 12),
        Paragraph("Academic Block 1 (AB1) is the primary academic building.", styles["Normal"]),
        Spacer(1, 12),
        table,
    ]
    doc.build(story)


def _make_blank_pdf(path) -> None:
    """A single blank page -- no extractable text at all."""
    doc = SimpleDocTemplate(str(path), pagesize=letter)
    doc.build([Spacer(1, 12)])


# ---------------------------------------------------------------------
# Group 1: structural tests (no heavy deps)
# ---------------------------------------------------------------------


def test_pdf_added_to_supported_extensions():
    from app.rag.document_loader import SUPPORTED_EXTENSIONS

    assert ".pdf" in SUPPORTED_EXTENSIONS


def test_multi_page_pdf_preserves_page_metadata(tmp_path):
    pdf_path = tmp_path / "multi_page.pdf"
    _make_text_pdf(
        pdf_path,
        [
            "Academic Block 1 is located on the north side of campus.",
            "Academic Block 2 is located on the south side of campus.",
            "The library is located at the center of campus.",
        ],
    )

    pages = _load_pdf_document(pdf_path)
    page_numbers = [p for p, _ in pages]
    assert page_numbers == sorted(page_numbers)
    assert page_numbers[0] == 1
    assert len(pages) == 3

    chunks = load_and_chunk_documents(tmp_path)
    assert chunks, "expected at least one chunk from the multi-page PDF"
    assert all(c.source == "multi_page.pdf" for c in chunks)
    assert all(c.page is not None for c in chunks)
    # Every chunk's metadata dict carries the page it came from.
    for c in chunks:
        meta = c.as_metadata_dict()
        assert meta["page"] == c.page
        assert meta["file_type"] == "pdf"
        assert meta["source"] == "multi_page.pdf"
    # Chunk IDs encode the page, so re-running is stable/deterministic (existing upsert-by-id dedup keeps working).
    assert all("::page-" in c.id for c in chunks)


def test_pdf_table_content_is_preserved_in_chunks(tmp_path):
    pdf_path = tmp_path / "building_table.pdf"
    _make_table_pdf(pdf_path)

    chunks = load_and_chunk_documents(tmp_path)
    combined_text = "\n".join(c.text for c in chunks)
    assert "AB1" in combined_text
    assert "45 m" in combined_text
    assert "AB2" in combined_text


def test_invalid_pdf_is_skipped_not_crashed(tmp_path, caplog):
    # A .txt file renamed to .pdf -- not a real PDF at all.
    fake_pdf = tmp_path / "not_really_a_pdf.pdf"
    fake_pdf.write_text("This is plain text, not a PDF.", encoding="utf-8")

    with pytest.raises(PDFExtractionError):
        _load_pdf_document(fake_pdf)

    # But the full pipeline must not crash -- it logs and skips.
    with caplog.at_level(logging.WARNING):
        chunks = load_and_chunk_documents(tmp_path)
    assert chunks == []
    assert any("not_really_a_pdf.pdf" in rec.message for rec in caplog.records)


def test_blank_pdf_produces_no_chunks_and_warns(tmp_path, caplog):
    pdf_path = tmp_path / "blank.pdf"
    _make_blank_pdf(pdf_path)

    pages = _load_pdf_document(pdf_path)
    assert pages == []

    with caplog.at_level(logging.WARNING):
        chunks = load_and_chunk_documents(tmp_path)
    assert chunks == []
    assert any("no extractable text" in rec.message for rec in caplog.records)


def test_existing_md_txt_behavior_unaffected_by_pdf_support(tmp_path):
    """TEST 7: adding PDF support must not change .md/.txt handling at all."""
    (tmp_path / "notes.md").write_text("Paragraph one.\n\nParagraph two about AB1.", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("Plain text notes about the mission.", encoding="utf-8")

    docs = load_raw_documents(tmp_path)
    names = {name for name, _ in docs}
    assert names == {"notes.md", "notes.txt"}

    chunks = load_and_chunk_documents(tmp_path)
    md_txt_chunks = [c for c in chunks if c.source in ("notes.md", "notes.txt")]
    assert len(md_txt_chunks) == 2
    for c in md_txt_chunks:
        # Original ID format is unchanged: "{filename}::chunk-{i}"
        assert c.id == f"{c.source}::chunk-{c.chunk_index}"
        assert c.page is None
        assert "page" not in c.as_metadata_dict()


def test_pdf_and_md_can_coexist_in_same_directory(tmp_path):
    (tmp_path / "notes.md").write_text("Some markdown notes.", encoding="utf-8")
    _make_text_pdf(tmp_path / "info.pdf", ["Some PDF content about AB1."])

    chunks = load_and_chunk_documents(tmp_path)
    sources = {c.source for c in chunks}
    assert sources == {"notes.md", "info.pdf"}


# ---------------------------------------------------------------------
# Group 2: full pipeline through real embeddings + a real Chroma collection
# ---------------------------------------------------------------------


@pytest.mark.heavy
def test_pdf_end_to_end_retrieval(tmp_path, monkeypatch):
    """TEST 1 + TEST 6: upload a PDF, build the EXISTING index, retrieve through the EXISTING RAG retriever."""
    import app.config as config_module
    from app.rag.embeddings import get_embedder
    from app.rag.retriever import build_index, retrieve

    get_embedder.cache_clear()

    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    _make_text_pdf(
        documents_dir / "VIT_Chennai_Campus_Building_Details.pdf",
        [
            "Academic Block 1 (AB1) is a seven floor building with a height of "
            "forty five meters. It is used for undergraduate engineering classes "
            "and is located near the main gate of VIT Chennai."
        ],
    )

    chroma_dir = tmp_path / "chroma"
    monkeypatch.setattr(config_module.settings, "documents_dir", documents_dir)
    monkeypatch.setattr(config_module.settings, "chroma_persist_dir", str(chroma_dir))
    monkeypatch.setattr(config_module.settings, "chroma_collection_name", "test_pdf_collection")

    build_index(force_rebuild=True)
    results = retrieve("What is the height of AB1?", k=3)

    assert results, "expected at least one retrieved chunk"
    assert any("AB1" in r.text or "45" in r.text for r in results)
    assert any(r.source == "VIT_Chennai_Campus_Building_Details.pdf" for r in results)

    get_embedder.cache_clear()
