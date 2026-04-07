"""Embedding models for resume RAG — local (free) or OpenAI.

Used by ChromaDB to embed resume chunks and JD queries for similarity search.
"""

from typing import Optional

import structlog

logger = structlog.get_logger()


class EmbeddingModel:
    """Configurable embedding model — local sentence-transformers or OpenAI."""

    def __init__(self, config: dict):
        embedding_config = config.get("llm", {}).get("embedding", {})
        self.provider = embedding_config.get("provider", "local")
        self.model_name = embedding_config.get("model", "all-MiniLM-L6-v2")
        self._model = None

    def _load_local(self):
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(self.model_name)
        logger.info("embedding_model_loaded", provider="local", model=self.model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts. Returns list of float vectors."""
        if self.provider == "local":
            if self._model is None:
                self._load_local()
            embeddings = self._model.encode(texts, show_progress_bar=False)
            return embeddings.tolist()

        elif self.provider == "openai":
            return self._embed_openai(texts)

        else:
            raise ValueError(f"Unknown embedding provider: {self.provider}")

    def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        """Embed via OpenAI API."""
        import openai
        client = openai.OpenAI()
        response = client.embeddings.create(model=self.model_name, input=texts)
        return [item.embedding for item in response.data]

    def get_chromadb_function(self):
        """Return an embedding function compatible with ChromaDB's interface."""
        model = self

        class ChromaEmbeddingAdapter:
            def __call__(self, input: list[str]) -> list[list[float]]:
                return model.embed(input)

        return ChromaEmbeddingAdapter()
