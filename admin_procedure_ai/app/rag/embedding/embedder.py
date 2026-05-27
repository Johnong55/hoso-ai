# app/rag/embedding/embedder.py
import uuid
from typing import Any

from google import genai
from google.genai import types as genai_types
from loguru import logger
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from app.core.config import settings
from app.rag.chunking.strategy import Chunk


# ── Singleton clients ─────────────────────────────────────────────────────────

_qdrant_client: QdrantClient | None = None
_gemini_client: genai.Client | None = None


def _get_qdrant_client() -> QdrantClient:
    global _qdrant_client
    if _qdrant_client is None:
        if settings.QDRANT_URL and settings.QDRANT_API_KEY:
            # ☁️  Qdrant Cloud
            _qdrant_client = QdrantClient(
                url=settings.QDRANT_URL,
                api_key=settings.QDRANT_API_KEY,
            )
        elif settings.QDRANT_PERSIST_DIR:
            # 💾 Local / embedded — no Docker needed
            _qdrant_client = QdrantClient(path=settings.QDRANT_PERSIST_DIR)
        else:
            # 🐳 Self-hosted Docker
            _qdrant_client = QdrantClient(
                host=settings.QDRANT_HOST,
                port=settings.QDRANT_PORT,
            )
        _ensure_collection(_qdrant_client)
    return _qdrant_client


def _ensure_collection(client: QdrantClient) -> None:
    """Create the collection if it doesn't exist yet."""
    existing = {c.name for c in client.get_collections().collections}
    if settings.QDRANT_COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            vectors_config=VectorParams(
                size=settings.EMBEDDING_DIMENSIONS,
                distance=Distance.COSINE,
            ),
        )
        logger.info(
            f"Qdrant | created collection '{settings.QDRANT_COLLECTION_NAME}' "
            f"| dims={settings.EMBEDDING_DIMENSIONS}"
        )


def _get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _gemini_client


# Cohere input_type → Gemini task_type
_TASK_TYPE_MAP = {
    "search_document": "RETRIEVAL_DOCUMENT",
    "search_query": "RETRIEVAL_QUERY",
}


# ── Embedder ──────────────────────────────────────────────────────────────────

class Embedder:
    """
    Embedding via Gemini (gemini-embedding-001) + Qdrant vector store.

    Gemini task_type convention:
      - "RETRIEVAL_DOCUMENT" → embed chunks before storing
      - "RETRIEVAL_QUERY"    → embed user query at search time
    """

    def __init__(self) -> None:
        self._qdrant = _get_qdrant_client()

    # ── Public API ────────────────────────────────────────────────────────────

    def embed_chunks(self, chunks: list[Chunk], source_id: str) -> list[dict[str, Any]]:
        """Embed a list of Chunk objects and upsert them into Qdrant."""
        if not chunks:
            return []

        texts = [c.content for c in chunks]
        vector_ids = [str(uuid.uuid4()) for _ in chunks]

        embeddings = self._get_embeddings(texts, input_type="search_document")

        points: list[PointStruct] = []
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "source_id": source_id,
                "chunk_type": chunk.chunk_type.value,
                "content": chunk.content,
                **{k: (v if v is not None else "") for k, v in chunk.metadata.items()},
            }
            points.append(PointStruct(
                id=vector_ids[i],
                vector=embeddings[i],
                payload=payload,
            ))

        self._qdrant.upsert(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            points=points,
        )
        logger.info(f"Embedder | upserted {len(chunks)} chunks | source_id={source_id}")

        return [
            {
                "vector_id": vector_ids[i],
                "content": chunks[i].content,
                "chunk_type": chunks[i].chunk_type,
                "metadata": chunks[i].metadata,
            }
            for i in range(len(chunks))
        ]

    def embed_query(self, query: str) -> list[float]:
        """Embed a user query string (RETRIEVAL_QUERY task_type)."""
        return self._get_embeddings([query], input_type="search_query")[0]

    def delete_by_source(self, source_id: str) -> None:
        """Delete all vectors whose payload.source_id matches."""
        self._qdrant.delete(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        FieldCondition(
                            key="source_id",
                            match=MatchValue(value=source_id),
                        )
                    ]
                )
            ),
        )
        logger.info(f"Embedder | deleted all vectors | source_id={source_id}")

    def delete_by_ids(self, vector_ids: list[str]) -> None:
        """Delete specific vectors by their UUID string IDs."""
        if not vector_ids:
            return
        self._qdrant.delete(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            points_selector=PointIdsList(points=vector_ids),
        )

    # ── Private ───────────────────────────────────────────────────────────────

    def _get_embeddings(
        self,
        texts: list[str],
        input_type: str = "search_document",
    ) -> list[list[float]]:
        """
        Call Gemini Embed API.
        Auto-batches when len(texts) > 100 (Gemini per-request limit for embed_content).
        """
        client = _get_gemini_client()
        task_type = _TASK_TYPE_MAP.get(input_type, "RETRIEVAL_DOCUMENT")
        batch_size = 100
        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i: i + batch_size]
            response = client.models.embed_content(
                model=settings.EMBEDDING_MODEL,
                contents=batch,
                config=genai_types.EmbedContentConfig(
                    task_type=task_type,
                    output_dimensionality=settings.EMBEDDING_DIMENSIONS,
                ),
            )
            all_embeddings.extend(e.values for e in response.embeddings)

        return all_embeddings
