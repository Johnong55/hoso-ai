# app/rag/retrieval/retriever.py
from dataclasses import dataclass
from typing import Any

import chromadb
from loguru import logger

from app.core.config import settings
from app.rag.embedding.embedder import _get_chroma_client


@dataclass
class RetrievedChunk:
    vector_id: str
    content: str
    score: float
    metadata: dict[str, Any]


class Retriever:
    """
    Performs pre-filtered vector search against ChromaDB.
    Pre-filter by metadata before vector search to reduce candidate set.
    """

    def __init__(self) -> None:
        self._chroma = _get_chroma_client()
        self._collection = self._chroma.get_or_create_collection(
            name=settings.CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

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
        Query Chroma with optional metadata pre-filters.
        Fetches top_k * 2 candidates, applies threshold, returns top_k.
        """
        where: dict[str, Any] | None = self._build_where(locality, domain, chunk_type)

        query_kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": min(top_k * 2, 50),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            query_kwargs["where"] = where

        try:
            results = self._collection.query(**query_kwargs)
        except Exception as exc:
            logger.error(f"Retriever | Chroma query failed | error={exc}")
            return []

        chunks: list[RetrievedChunk] = []
        ids = results["ids"][0] if results["ids"] else []
        docs = results["documents"][0] if results["documents"] else []
        metas = results["metadatas"][0] if results["metadatas"] else []
        distances = results["distances"][0] if results["distances"] else []

        for vec_id, doc, meta, dist in zip(ids, docs, metas, distances):
            # Chroma cosine distance → similarity score
            score = 1.0 - dist
            if score < score_threshold:
                continue
            chunks.append(RetrievedChunk(
                vector_id=vec_id,
                content=doc,
                score=score,
                metadata=meta,
            ))

        chunks.sort(key=lambda c: c.score, reverse=True)
        result = chunks[:top_k]
        logger.debug(
            f"Retriever | retrieved {len(result)}/{len(chunks)} chunks "
            f"| threshold={score_threshold} | locality={locality} | domain={domain}"
        )
        return result

    def _build_where(
        self,
        locality: str | None,
        domain: str | None,
        chunk_type: str | None,
    ) -> dict[str, Any] | None:
        conditions: list[dict[str, Any]] = []

        if locality:
            conditions.append({"locality": {"$eq": locality}})
        if domain:
            conditions.append({"domain": {"$eq": domain}})
        if chunk_type:
            conditions.append({"chunk_type": {"$eq": chunk_type}})

        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}
