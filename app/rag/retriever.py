"""
Phase 2 -- vector index + retrieval.

Pipeline implemented in this file:

    data/documents/*.md
            |
    document_loader.load_and_chunk_documents()
            |
    embeddings.get_embedder().embed_texts()
            |
    Chroma collection (persisted at settings.chroma_persist_path)
            |
    retrieve(query) -> ranked chunks with source + distance

We always pass pre-computed embeddings into Chroma (both when indexing
and when querying) rather than letting Chroma embed text itself. That
keeps exactly one embedding model in play -- the one configured via
`EMBEDDING_MODEL` in `.env` -- instead of Chroma silently downloading and
using its own default embedding function if we ever forgot to pass
`query_embeddings`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import settings
from app.rag.document_loader import Chunk, load_and_chunk_documents
from app.rag.embeddings import get_embedder

log = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    text: str
    source: str
    chunk_index: int
    distance: float  # lower = more similar (Chroma's default is squared L2 / cosine depending on space)
    # PDF-support follow-up: the PDF page a chunk came from (document_loader.py
    # stores it in Chroma metadata), None for .md/.txt -- optional, default None,
    # so every existing RetrievedChunk(...) construction is unaffected.
    page: int | None = None


def _get_client():
    # Imported lazily -- same reasoning as the sentence-transformers
    # import in embeddings.py: don't force chromadb's import cost onto
    # every caller of this module, only onto callers that actually index
    # or query.
    import chromadb

    settings.chroma_persist_path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(settings.chroma_persist_path))


def _get_collection():
    client = _get_client()
    # No embedding_function is registered here because we always supply
    # embeddings explicitly on both add() and query() below -- Chroma
    # only calls a collection's embedding function when it has to embed
    # text itself, which never happens in this project's code path.
    return client.get_or_create_collection(name=settings.chroma_collection_name)


def collection_is_empty() -> bool:
    return _get_collection().count() == 0


def build_index(force_rebuild: bool = False) -> int:
    """
    Load, chunk, embed, and store every document in `data/documents/`.

    Returns the number of chunks indexed. Safe to call repeatedly --
    Chroma's `add()` upserts by id, so re-running this after editing a
    document just overwrites that document's chunks (as long as chunk
    ids are stable, which they are: `"<filename>::chunk-<i>"`).

    `force_rebuild=True` deletes the collection first, which matters if
    a document was *removed* or its chunk count shrank (otherwise old
    chunks with no corresponding new chunk id would linger).
    """
    client = _get_client()

    if force_rebuild:
        try:
            client.delete_collection(name=settings.chroma_collection_name)
            log.info("Deleted existing collection %r for a clean rebuild.", settings.chroma_collection_name)
        except Exception:
            pass  # collection didn't exist yet -- fine

    collection = client.get_or_create_collection(name=settings.chroma_collection_name)

    chunks: list[Chunk] = load_and_chunk_documents()
    if not chunks:
        log.warning("No chunks to index -- add files to data/documents/ first.")
        return 0

    embedder = get_embedder()
    vectors = embedder.embed_texts([c.text for c in chunks])

    collection.add(
        ids=[c.id for c in chunks],
        documents=[c.text for c in chunks],
        embeddings=vectors,
        metadatas=[c.as_metadata_dict() for c in chunks],
    )

    log.info(
        "Indexed %d chunk(s) into Chroma collection %r at %s",
        len(chunks),
        settings.chroma_collection_name,
        settings.chroma_persist_path,
    )
    return len(chunks)


def retrieve(query: str, k: int | None = None) -> list[RetrievedChunk]:
    """
    Embed `query` and return the top-`k` most relevant chunks from the
    knowledge base (k defaults to settings.rag_top_k).

    Example:
        >>> retrieve("Take off and fly to three waypoints at 50 meters.")
        [RetrievedChunk(text="... MAV_CMD_NAV_TAKEOFF ...", source="mavlink_commands.md", ...), ...]
    """
    k = k or settings.rag_top_k
    collection = _get_collection()

    if collection.count() == 0:
        log.warning(
            "Chroma collection %r is empty -- call build_index() first "
            "(e.g. `python -m app.rag.retriever --build`).",
            settings.chroma_collection_name,
        )
        return []

    embedder = get_embedder()
    query_vector = embedder.embed_query(query)

    results = collection.query(
        query_embeddings=[query_vector],
        n_results=min(k, collection.count()),
        include=["documents", "metadatas", "distances"],
    )

    retrieved: list[RetrievedChunk] = []
    documents = results["documents"][0]
    metadatas = results["metadatas"][0]
    distances = results["distances"][0]
    for text, meta, dist in zip(documents, metadatas, distances):
        retrieved.append(
            RetrievedChunk(
                text=text,
                source=meta.get("source", "unknown"),
                chunk_index=meta.get("chunk_index", -1),
                distance=dist,
                page=meta.get("page"),
            )
        )
    return retrieved


def format_context(chunks: list[RetrievedChunk]) -> str:
    """
    Render retrieved chunks into a single string suitable for dropping
    into an LLM prompt (used by Phase 3's generator). Each chunk is
    labeled with its source file so the LLM (and a human debugging
    output) can see where a fact came from.
    """
    if not chunks:
        return "(no relevant context retrieved)"
    parts = []
    for c in chunks:
        page = getattr(c, "page", None)
        label = f"{c.source}, page {page}" if page is not None else c.source
        parts.append(f"[Source: {label}]\n{c.text}")
    return "\n\n---\n\n".join(parts)


def _demo_cli() -> None:
    """
    Manual test entry point:
        python -m app.rag.retriever --build          # (re)build the index
        python -m app.rag.retriever "your question"   # retrieve for a query
    """
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    args = sys.argv[1:]
    if not args or args[0] == "--build":
        n = build_index(force_rebuild=True)
        print(f"Indexed {n} chunks.")
        if not args:
            return

    query = " ".join(a for a in args if a != "--build")
    if not query:
        query = "Take off and fly to three waypoints at 50 meters."
        print(f"(no query given, using default) {query!r}\n")

    chunks = retrieve(query)
    print(f"\nQuery: {query}\n")
    print(f"Retrieved {len(chunks)} chunk(s):\n")
    for i, c in enumerate(chunks, 1):
        print(f"--- [{i}] {c.source} (chunk {c.chunk_index}, distance={c.distance:.4f}) ---")
        print(c.text[:400] + ("..." if len(c.text) > 400 else ""))
        print()


if __name__ == "__main__":
    _demo_cli()
