# app/rag/pipeline.py
import time
from dataclasses import dataclass, field

from loguru import logger

from app.core.config import settings
from app.rag.embedding.embedder import Embedder
from app.rag.generation.generator import GenerationResult, Generator
from app.rag.retrieval.retriever import RetrievedChunk, Retriever


@dataclass
class PipelineResult:
    answer: str
    is_fallback: bool
    chunks: list[RetrievedChunk]
    rewritten_query: str
    generation: GenerationResult
    latency_ms: int


class RAGPipeline:
    """
    Orchestrates: query rewrite → embed → retrieve → generate.
    Single entry point for the entire RAG flow.
    """

    def __init__(self) -> None:
        self._embedder = Embedder()
        self._retriever = Retriever()
        self._generator = Generator()

    async def run(
        self,
        query: str,
        locality: str | None = None,
        domain: str | None = None,
        conversation_history: list[dict] | None = None,
    ) -> PipelineResult:
        start = time.monotonic()

        # Step 1: Rewrite query for better retrieval
        rewritten = self._generator.rewrite_query(query, conversation_history)
        logger.debug(f"RAG | rewritten_query={rewritten!r}")

        # Step 2: Embed the (rewritten) query
        query_embedding = self._embedder.embed_query(rewritten)

        # Step 3: Retrieve with pre-filtering
        chunks = self._retriever.retrieve(
            query_embedding=query_embedding,
            top_k=settings.RAG_TOP_K,
            score_threshold=settings.RAG_SCORE_THRESHOLD,
            locality=locality,
            domain=domain,
        )

        # Cap to max context chunks
        chunks = chunks[: settings.RAG_MAX_CONTEXT_CHUNKS]

        # Step 4: Generate answer
        generation = self._generator.generate(rewritten, chunks)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            f"RAG | pipeline complete | chunks={len(chunks)} "
            f"| fallback={generation.is_fallback} | latency={elapsed_ms}ms"
        )

        return PipelineResult(
            answer=generation.answer,
            is_fallback=generation.is_fallback,
            chunks=chunks,
            rewritten_query=rewritten,
            generation=generation,
            latency_ms=elapsed_ms,
        )
