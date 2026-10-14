"""
Phase 2 tests -- run with:  pytest tests/test_phase2_rag.py -v

Split into two groups on purpose:

1. Document-loading/chunking tests -- pure Python, no extra dependencies
   beyond the standard library, so they run anywhere this repo runs.
2. Embedding/retrieval tests -- these need `sentence-transformers`,
   `torch`, and `chromadb` actually installed (they download/load a real
   model on first run, which is slow and needs network access the first
   time). They're marked so you can skip them in a constrained
   environment: `pytest tests/test_phase2_rag.py -v -m "not heavy"`.
"""

import pytest

from app.rag.document_loader import chunk_text, load_and_chunk_documents, load_raw_documents

# ---------------------------------------------------------------------
# Group 1: document loading & chunking (no heavy deps)
# ---------------------------------------------------------------------


def test_load_raw_documents_finds_knowledge_base_files():
    docs = load_raw_documents()
    names = {name for name, _ in docs}
    # These five ship with the project (Phase 2) -- if a name changes,
    # update this test alongside it.
    expected = {
        "mavlink_commands.md",
        "px4_mission_requirements.md",
        "waypoints_and_sequencing.md",
        "safety_constraints.md",
        "mavsdk_overview.md",
    }
    assert expected.issubset(names), f"missing docs: {expected - names}"


def test_load_raw_documents_raises_on_missing_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_raw_documents(documents_dir=tmp_path / "does_not_exist")


def test_chunk_text_respects_size_limit():
    text = " ".join(f"word{i}" for i in range(500))
    pieces = chunk_text(text, chunk_size_words=100, overlap_words=20)
    assert len(pieces) > 1
    for p in pieces:
        assert len(p.split()) <= 100


def test_chunk_text_overlap_shares_words_between_consecutive_chunks():
    text = " ".join(f"word{i}" for i in range(300))
    pieces = chunk_text(text, chunk_size_words=100, overlap_words=20)
    first_words = pieces[0].split()
    second_words = pieces[1].split()
    # last `overlap_words` of chunk 1 should reappear at the start of chunk 2
    assert first_words[-20:] == second_words[:20]


def test_chunk_text_rejects_invalid_size_overlap_combo():
    with pytest.raises(ValueError):
        chunk_text("some text", chunk_size_words=10, overlap_words=10)


def test_chunk_text_empty_input_returns_empty_list():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_load_and_chunk_documents_produces_unique_stable_ids():
    chunks = load_and_chunk_documents()
    assert len(chunks) > 0
    ids = [c.id for c in chunks]
    assert len(ids) == len(set(ids)), "chunk ids must be unique"
    for c in chunks:
        assert c.id == f"{c.source}::chunk-{c.chunk_index}"


# ---------------------------------------------------------------------
# Group 2: embeddings + Chroma retrieval (heavy deps, real model load)
# ---------------------------------------------------------------------


@pytest.mark.heavy
def test_embedder_produces_correct_vector_shape():
    from app.rag.embeddings import get_embedder

    embedder = get_embedder()
    vectors = embedder.embed_texts(["take off and fly to a waypoint", "return to launch"])
    assert len(vectors) == 2
    assert len(vectors[0]) == embedder.dimension
    assert len(vectors[0]) > 0


@pytest.mark.heavy
def test_build_index_and_retrieve_end_to_end():
    from app.rag.retriever import build_index, retrieve

    n = build_index(force_rebuild=True)
    assert n > 0

    results = retrieve("Take off and fly to three waypoints at 50 meters.", k=3)
    assert len(results) > 0
    # This query touches takeoff, waypoints, and altitude -- all three
    # live across mavlink_commands.md, px4_mission_requirements.md, and
    # waypoints_and_sequencing.md, and which one embeds "closest" for a
    # given MiniLM model/chunking is not something to hard-pin a test to
    # (that's brittle against a model/document edit, not a real bug).
    # What must hold is that retrieval actually returns *relevant*
    # knowledge-base content rather than, say, only safety_constraints.md
    # or mavsdk_overview.md chunks that don't mention this query's topic.
    sources = {r.source for r in results}
    relevant_sources = {
        "mavlink_commands.md",
        "px4_mission_requirements.md",
        "waypoints_and_sequencing.md",
    }
    assert sources & relevant_sources, (
        f"expected at least one mission/waypoint-relevant document, got {sources}"
    )


@pytest.mark.heavy
def test_retrieve_finds_takeoff_command_for_a_targeted_query():
    """
    A narrower, less ambiguous query than the end-to-end test above --
    this one should reliably surface mavlink_commands.md specifically,
    since nothing else in the knowledge base defines what MAV_CMD_NAV_TAKEOFF is.
    """
    from app.rag.retriever import build_index, retrieve

    build_index(force_rebuild=True)
    results = retrieve("What MAVLink command is used to command a drone to take off?", k=2)
    assert len(results) > 0
    sources = {r.source for r in results}
    assert "mavlink_commands.md" in sources


@pytest.mark.heavy
def test_retrieve_on_empty_collection_returns_empty_list_not_error(tmp_path, monkeypatch):
    from app import config as config_module
    from app.rag import retriever

    # Point at a fresh, empty Chroma dir so this test doesn't depend on
    # whatever the real index currently contains.
    monkeypatch.setattr(config_module.settings, "chroma_persist_dir", str(tmp_path / "empty_chroma"))
    monkeypatch.setattr(config_module.settings, "chroma_collection_name", "empty_test_collection")

    results = retriever.retrieve("anything")
    assert results == []
