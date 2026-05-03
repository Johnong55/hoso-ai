# app/rag/embedding/embedder.py
import uuid
from typing import Any

import chromadb
import cohere
from loguru import logger

from app.core.config import settings
from app.rag.chunking.strategy import Chunk


def _get_chroma_client() -> chromadb.ClientAPI:
    if settings.ENVIRONMENT == "production":
        return chromadb.HttpClient(host=settings.CHROMA_HOST, port=settings.CHROMA_PORT)
    return chromadb.PersistentClient(path=settings.CHROMA_PERSIST_DIR)


# Singleton Cohere client
_cohere_client: cohere.Client | None = None


def _get_cohere_client() -> cohere.Client:
    global _cohere_client
    if _cohere_client is None:
        _cohere_client = cohere.Client(api_key=settings.COHERE_API_KEY)
    return _cohere_client


class Embedder:
    """
    Embedding via Cohere (embed-multilingual-v3.0) + ChromaDB.
    Cohere yêu cầu input_type:
      - "search_document" khi embed chunks để lưu vào DB
      - "search_query"    khi embed câu hỏi của user
    """

    def __init__(self) -> None:
        self._chroma = _get_chroma_client()
        self._collection = self._chroma.get_or_create_collection(
            name=settings.CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    def embed_chunks(self, chunks: list[Chunk], source_id: str) -> list[dict[str, Any]]:
        if not chunks:
            return []

        texts = [c.content for c in chunks]
        vector_ids = [str(uuid.uuid4()) for _ in chunks]

        # input_type="search_document" khi lưu vào vector store
        embeddings = self._get_embeddings(texts, input_type="search_document")

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
        # input_type="search_query" khi embed câu hỏi
        return self._get_embeddings([query], input_type="search_query")[0]

    def delete_by_source(self, source_id: str) -> None:
        results = self._collection.get(where={"source_id": source_id})
        if results["ids"]:
            self._collection.delete(ids=results["ids"])
            logger.info(f"Embedder | deleted {len(results['ids'])} vectors | source_id={source_id}")

    def delete_by_ids(self, vector_ids: list[str]) -> None:
        if vector_ids:
            self._collection.delete(ids=vector_ids)

    def _get_embeddings(
        self,
        texts: list[str],
        input_type: str = "search_document",
    ) -> list[list[float]]:
        """
        Gọi Cohere Embed API.
        Tự động batch nếu > 96 texts (giới hạn của Cohere).
        """
        client = _get_cohere_client()
        batch_size = 96  # Cohere max per request
        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i: i + batch_size]
            response = client.embed(
                texts=batch,
                model=settings.EMBEDDING_MODEL,
                input_type=input_type,
                embedding_types=["float"],
            )
            all_embeddings.extend(response.embeddings.float)

        return all_embeddings
