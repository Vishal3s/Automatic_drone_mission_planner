"""
Phase 2 -- document loading and chunking.

Reads every `.md`/`.txt` file in `data/documents/` (see `app/config.py`'s
`settings.documents_dir`) and splits each into overlapping word-based
chunks small enough to embed and retrieve individually.

Why word-based chunking rather than a fancier splitter: the knowledge
base here is a handful of short, hand-written markdown files (a few
hundred words each), not a large unstructured corpus. A simple,
dependency-free chunker is easier to reason about and test than pulling
in a text-splitting library for a problem this small — if the knowledge
base grows substantially in a later phase, swapping this for
`langchain`'s or `llama-index`'s splitter is a contained change (only
this file), because `retriever.py` only depends on the `Chunk` shape
below, not on how chunks were produced.

--- PDF support (feature addition) -------------------------------------

`.pdf` is handled by a SEPARATE extraction path (`_load_pdf_document()`
below) that still funnels into the SAME, completely unmodified
`chunk_text()` used by `.md`/`.txt` -- called once per PDF page, so each
chunk carries exactly one page's `page` number and a chunk never spans
two pages. `retriever.py` and `embeddings.py` needed zero changes: both
already only depend on `Chunk`'s shape / a flat metadata dict, not on
how a chunk was produced (see the paragraph above -- this is that exact
contained change happening).

Requires `pdfplumber` (added to requirements.txt). Import is lazy
(inside the function that needs it), matching this project's existing
pattern in `embeddings.py`/`retriever.py` for optional heavy deps, so
importing this module never forces pdfplumber/pdfminer to load for
projects only using `.md`/`.txt`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf"}


@dataclass
class Chunk:
    """One retrievable unit of the knowledge base."""

    id: str
    text: str
    source: str          # filename the chunk came from, e.g. "mavlink_commands.md"
    chunk_index: int      # position of this chunk within its source file
    page: int | None = None  # 1-based PDF page number; None for non-paginated sources (.md/.txt)
    metadata: dict = field(default_factory=dict)

    def as_metadata_dict(self) -> dict:
        """
        Flat dict suitable for Chroma's `metadatas=` (no nested values,
        and no `None` values -- Chroma rejects those, so `page` is only
        included when it's actually known).
        """
        d = {"source": self.source, "chunk_index": self.chunk_index, "file_type": Path(self.source).suffix.lstrip(".") or "unknown"}
        if self.page is not None:
            d["page"] = self.page
        d.update(self.metadata)
        return d


_TEXT_EXTENSIONS = {".md", ".txt"}  # plain-text extensions read via load_raw_documents(); .pdf is binary, handled separately below


def load_raw_documents(documents_dir: Path | None = None) -> list[tuple[str, str]]:
    """
    Read every supported PLAIN-TEXT file in `documents_dir` (`.md`/`.txt`
    -- unchanged Phase 2 behavior). `.pdf` files are binary and are
    handled separately by `_load_pdf_document()` / `load_and_chunk_documents()`
    below, since `path.read_text()` would garble or crash on them.

    Returns a list of (filename, full_text) tuples, sorted by filename so
    indexing order is deterministic (helpful for reproducible tests).
    """
    documents_dir = documents_dir or settings.documents_dir
    if not documents_dir.exists():
        raise FileNotFoundError(
            f"Knowledge-base folder does not exist: {documents_dir}. "
            f"Expected files like mavlink_commands.md there."
        )

    docs: list[tuple[str, str]] = []
    for path in sorted(documents_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in _TEXT_EXTENSIONS:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                docs.append((path.name, text))
            else:
                log.warning("Skipping empty document: %s", path.name)

    if not docs:
        log.warning(
            "No .md/.txt documents found in %s (any .pdf files are loaded separately).", documents_dir
        )
    return docs


def _render_table_as_text(table: list[list[str | None]]) -> str:
    """
    Render one pdfplumber-extracted table (`page.extract_tables()` -- a
    list of rows, each a list of cell strings, `None` for empty cells)
    into simple pipe-delimited text so it's preserved in a form useful
    for semantic retrieval, e.g.:

        Building | Floors | Height
        AB1 | 7 | 45 m

    Spec requirement: "do not silently discard tables." This is
    PDF-specific preprocessing spliced into a page's text BEFORE the
    existing `chunk_text()` runs -- not a second retrieval system.
    """
    lines = []
    for row in table:
        cells = [(c or "").strip() for c in row]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _clean_pdf_text(text: str) -> str:
    """
    Minimal PDF-specific whitespace normalization, scoped to the PDF
    path only (the existing `.md`/`.txt` path is untouched). Collapses
    runs of blank lines pdfplumber sometimes emits while preserving the
    blank-line paragraph breaks `chunk_text()` splits on.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    cleaned: list[str] = []
    blank_run = 0
    for ln in lines:
        if ln.strip():
            cleaned.append(ln)
            blank_run = 0
        else:
            blank_run += 1
            if blank_run == 1:
                cleaned.append("")
    return "\n".join(cleaned).strip()


def _load_pdf_document(path: Path) -> list[tuple[int, str]]:
    """
    Extract text from one PDF, page by page, using pdfplumber (the
    project's single chosen PDF library -- see module docstring).

    Returns a list of (page_number, cleaned_text) tuples, 1-based page
    numbers, one entry per page that has any extractable text (blank
    pages are simply omitted, not errors).

    Raises `PDFExtractionError` (defined below) for anything that
    prevents opening/reading the PDF at all (corrupted file, a
    non-PDF file renamed to `.pdf`, password-protected PDF) -- the
    caller (`load_and_chunk_documents()`) catches this, logs a
    warning, and SKIPS the file, exactly like the existing behavior
    for an empty `.md`/`.txt` file (one bad document never crashes the
    whole indexing run).
    """
    import pdfplumber  # lazy import -- see module docstring

    pages: list[tuple[int, str]] = []
    try:
        with pdfplumber.open(path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                try:
                    raw_text = page.extract_text() or ""
                except Exception as exc:  # pdfplumber/pdfminer can raise a variety of parse errors per-page
                    log.warning("Failed to extract text from %s page %d: %s -- skipping that page.", path.name, page_num, exc)
                    raw_text = ""

                try:
                    tables = page.extract_tables() or []
                except Exception as exc:
                    log.warning("Failed to extract tables from %s page %d: %s -- table content on that page may be missing.", path.name, page_num, exc)
                    tables = []

                table_text = "\n\n".join(_render_table_as_text(t) for t in tables if t)
                combined = "\n\n".join(part for part in (raw_text.strip(), table_text.strip()) if part)
                cleaned = _clean_pdf_text(combined)
                if cleaned:
                    pages.append((page_num, cleaned))
    except PDFExtractionError:
        raise
    except Exception as exc:
        # Covers pdfplumber/pdfminer's various failure modes for a file
        # that can't be opened at all: corrupted PDF, a non-PDF file
        # renamed to .pdf, password-protected PDF, unsupported/malformed
        # structure, etc. -- all surfaced uniformly as PDFExtractionError.
        raise PDFExtractionError(f"Could not open/parse {path.name} as a PDF: {exc}") from exc

    return pages


class PDFExtractionError(Exception):
    """Raised when a `.pdf` file cannot be opened or parsed at all (corrupt, password-protected, not really a PDF, ...)."""


def chunk_text(
    text: str,
    chunk_size_words: int = 180,
    overlap_words: int = 30,
) -> list[str]:
    """
    Split `text` into overlapping chunks of roughly `chunk_size_words`
    words each, breaking on paragraph boundaries where possible so a
    chunk doesn't cut a sentence in half more than necessary.

    `overlap_words` repeats the tail of each chunk at the start of the
    next one, so a fact stated right at a chunk boundary is still fully
    present in at least one chunk instead of being split across two.
    """
    if chunk_size_words <= overlap_words:
        raise ValueError("chunk_size_words must be greater than overlap_words")

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    words: list[str] = []
    for p in paragraphs:
        words.extend(p.split())
        words.append("\n")  # paragraph boundary marker, rejoined as a space below

    if not words:
        return []

    chunks: list[str] = []
    start = 0
    step = chunk_size_words - overlap_words
    while start < len(words):
        window = words[start : start + chunk_size_words]
        chunk = " ".join(w for w in window if w != "\n").strip()
        if chunk:
            chunks.append(chunk)
        if start + chunk_size_words >= len(words):
            break
        start += step

    return chunks


def load_and_chunk_documents(documents_dir: Path | None = None) -> list[Chunk]:
    """
    Full loading pipeline: read every document (`.md`/`.txt` AND, as of
    the PDF-support feature, `.pdf`), chunk it, and return a flat list
    of `Chunk` objects ready for embedding.

    `.md`/`.txt` handling below is byte-for-byte the original Phase 2
    logic (same chunk IDs, same call to `chunk_text()`) -- completely
    unchanged. `.pdf` handling is new: `chunk_text()` is called once
    PER PAGE (reusing it unmodified) so every chunk carries the `page`
    it came from and never spans two pages; a PDF that fails to open or
    has no extractable text anywhere is logged and SKIPPED, never
    silently indexed with fabricated content.
    """
    dir_path = documents_dir or settings.documents_dir
    all_chunks: list[Chunk] = []

    for filename, text in load_raw_documents(documents_dir):
        pieces = chunk_text(text)
        for i, piece in enumerate(pieces):
            all_chunks.append(
                Chunk(
                    id=f"{filename}::chunk-{i}",
                    text=piece,
                    source=filename,
                    chunk_index=i,
                )
            )

    if dir_path.exists():
        for path in sorted(dir_path.iterdir()):
            if not (path.is_file() and path.suffix.lower() == ".pdf"):
                continue
            try:
                pages = _load_pdf_document(path)
            except PDFExtractionError as exc:
                log.warning("Skipping unreadable PDF %s: %s", path.name, exc)
                continue

            if not pages:
                log.warning(
                    "PDF %s produced no extractable text on any page -- skipping. "
                    "This may be a scanned/image-based PDF requiring OCR, which this project does not yet implement.",
                    path.name,
                )
                continue

            i = 0
            for page_num, page_text in pages:
                for piece in chunk_text(page_text):
                    all_chunks.append(
                        Chunk(
                            id=f"{path.name}::page-{page_num}::chunk-{i}",
                            text=piece,
                            source=path.name,
                            chunk_index=i,
                            page=page_num,
                        )
                    )
                    i += 1

    log.info(
        "Loaded %d chunk(s) from %d document(s) in %s",
        len(all_chunks),
        len({c.source for c in all_chunks}),
        dir_path,
    )
    return all_chunks
