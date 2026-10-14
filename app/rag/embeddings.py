"""
Phase 2 -- embedding model wrapper.

Wraps `sentence-transformers` behind a small interface so the rest of the
app never imports `SentenceTransformer` directly. Two reasons:

1. The model is genuinely expensive to load (multi-second, loads weights
   from disk/HF cache) -- it should be loaded exactly once per process,
   not once per call. `get_embedder()` is a cached singleton for that
   reason.
2. If the embedding model is ever swapped for something that isn't a
   sentence-transformers model, only this file changes -- `retriever.py`
   and everything above it only calls `embed_texts()` / `embed_query()`.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from app.config import settings

log = logging.getLogger(__name__)


class Embedder:
    """Thin wrapper around a sentence-transformers model."""

    def __init__(self, model_name: str | None = None):
        # Imported lazily so `import app.rag.embeddings` (e.g. for type
        # hints elsewhere) doesn't force-load torch/sentence-transformers
        # unless an Embedder is actually constructed.
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name or settings.embedding_model
        log.info("Loading embedding model: %s (first load can take a while)", self.model_name)
        self._model = SentenceTransformer(self.model_name)
        log.info("Embedding model loaded. Vector size: %d", self.dimension)

    @property
    def dimension(self) -> int:
        # sentence-transformers renamed this method in the v6 line
        # (get_sentence_embedding_dimension -> get_embedding_dimension).
        # Prefer the new name; fall back to the old one so this keeps
        # working if the installed version is older than 6.0.
        if hasattr(self._model, "get_embedding_dimension"):
            return self._model.get_embedding_dimension()
        return self._model.get_sentence_embedding_dimension()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents/chunks for indexing."""
        if not texts:
            return []
        vectors = self._model.encode(texts, show_progress_bar=False, convert_to_numpy=True)
        return vectors.tolist()

    def embed_query(self, query: str) -> list[float]:
        """Embed a single user query for retrieval."""
        vector = self._model.encode([query], show_progress_bar=False, convert_to_numpy=True)
        return vector[0].tolist()


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """
    Process-wide singleton. `lru_cache` on a zero-argument function is a
    simple, standard-library way to get "load once, reuse everywhere"
    without a manual global variable.
    """
    return Embedder()
