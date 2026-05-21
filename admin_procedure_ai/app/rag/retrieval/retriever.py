# app/rag/retrieval/retriever.py
from dataclasses import dataclass
from typing import Any

from loguru import logger
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.core.config import settings
from app.rag.embedding.embedder import _get_qdrant_client


@dataclass
class RetrievedChunk:
    vector_id: str
    content: str
    score: float
    metadata: dict[str, Any]


class Retriever:
    """
    Performs pre-filtered vector search against Qdrant.
    Optional metadata filters (locality, domain, chunk_type) narrow the
    candidate set before scoring, then score_threshold trims low-quality hits.
    """

    def __init__(self) -> None:
        self._qdrant = _get_qdrant_client()

    def retrieve(
        self,
        query_embedding: list[float],
        top_k: int = settings.RAG_TOP_K,
        score_threshold: float = settings.RAG_SCORE_THRESHOLD,
        locality: str | None = None,
        domain: str | None = None,
        chunk_type: str | None = None,
    ) -> list[RetrievedChunk]:
        """
        Query Qdrant with optional payload pre-filters.
        Fetches top_k * 2 candidates, applies threshold, returns top_k.
        """
        query_filter = self._build_filter(locality, domain, chunk_type)

        try:
            # qdrant-client 1.12+: .search() deprecated → dùng .query_points()
            response = self._qdrant.query_points(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                query=query_embedding,
                limit=min(top_k * 2, 50),
                score_threshold=score_threshold,
                query_filter=query_filter,
                with_payload=True,
            )
            results = response.points
        except Exception as exc:
            logger.error(f"Retriever | Qdrant search failed | error={exc}")
            return []

        chunks: list[RetrievedChunk] = []
        for hit in results:
            payload = hit.payload or {}
            content = payload.pop("content", "")
            chunks.append(RetrievedChunk(
                vector_id=str(hit.id),
                content=content,
                score=hit.score,
                metadata=payload,
            ))

        chunks.sort(key=lambda c: c.score, reverse=True)
        result = chunks[:top_k]
        logger.debug(
            f"Retriever | retrieved {len(result)}/{len(chunks)} chunks "
            f"| threshold={score_threshold} | locality={locality} | domain={domain}"
        )
        return result

    # ── Private ───────────────────────────────────────────────────────────────

    def _build_filter(
        self,
        locality: str | None,
        domain: str | None,
        chunk_type: str | None,
    ) -> Filter | None:
        """Build a Qdrant Filter from optional metadata constraints."""
        conditions: list[FieldCondition] = []

        if locality:
            conditions.append(
                FieldCondition(key="locality", match=MatchValue(value=locality))
            )
        if domain:
            conditions.append(
                FieldCondition(key="domain", match=MatchValue(value=domain))
            )
        if chunk_type:
            conditions.append(
                FieldCondition(key="chunk_type", match=MatchValue(value=chunk_type))
            )

        if not conditions:
            return None
        return Filter(must=conditions)
