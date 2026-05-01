# app/rag/embedding/embedder.py
import uuid
from typing import Any

import chromadb
from loguru import logger
from openai import OpenAI

from app.core.config import settings
from app.rag.chunking.strategy import Chunk


def _get_chroma_client() -> chromadb.ClientAPI:
    """Returns a Chroma client (local persist or HTTP server based on config)."""
    if settings.ENVIRONMENT == "production":
        return chromadb.HttpClient(host=settings.CHROMA_HOST, port=settings.CHROMA_PORT)
    return chromadb.PersistentClient(path=settings.CHROMA_PERSIST_DIR)


class Embedder:
    """
    Handles embedding generation via OpenAI and syncing to ChromaDB.
    Chroma stores embeddings; MySQL stores only the vector_id reference.
    """

    def __init__(self) -> None:
        self._openai = OpenAI(api_key=settings.OPENAI_API_KEY)
        self._chroma = _get_chroma_client()
        self._collection = self._chroma.get_or_create_collection(
            name=settings.CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    def embed_chunks(self, chunks: list[Chunk], source_id: str) -> list[dict[str, Any]]:
        """
        Embed a list of chunks and upsert into Chroma.
        Returns list of dicts with vector_id, content, chunk_type, metadata.
        """
        if not chunks:
            return []

        texts = [c.content for c in chunks]
        vector_ids = [str(uuid.uuid4()) for _ in chunks]

        embeddings = self._get_embeddings(texts)

        chroma_metadatas = []
        for chunk in chunks:
            meta: dict[str, Any] = {
                "source_id": source_id,
                "chunk_type": chunk.chunk_type.value,
                **{k: (v if v is not None else "") for k, v in chunk.metadata.items()},
            }
            chroma_metadatas.append(meta)

        self._collection.upsert(
            ids=vector_ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=chroma_metadatas,
        )
        logger.info(f"Embedder | upserted {len(chunks)} chunks to Chroma | source_id={source_id}")

        return [
            {
                "vector_id": vector_ids[i],
                "content": chunks[i].content,
                "chunk_type": chunks[i].chunk_type,
                "metadata": chunks[i].metadata,
            }
            for i in range(len(chunks))
        ]

    def delete_by_source(self, source_id: str) -> None:
        """Delete all Chroma records for a given source (before re-embedding)."""
        results = self._collection.get(where={"source_id": source_id})
        if results["ids"]:
            self._collection.delete(ids=results["ids"])
            logger.info(f"Embedder | deleted {len(results['ids'])} vectors | source_id={source_id}")

    def delete_by_ids(self, vector_ids: list[str]) -> None:
        if vector_ids:
            self._collection.delete(ids=vector_ids)

    def embed_query(self, query: str) -> list[float]:
        return self._get_embeddings([query])[0]

    def _get_embeddings(self, texts: list[str]) -> list[list[float]]:
        response = self._openai.embeddings.create(
            model=settings.OPENAI_EMBEDDING_MODEL,
            input=texts,
        )
        return [item.embedding for item in response.data]
