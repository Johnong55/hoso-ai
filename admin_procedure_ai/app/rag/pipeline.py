# app/rag/pipeline.py
import time
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.db.base import AsyncSessionLocal
from app.models.document import ChunkType, DocumentChunk
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

        # Step 3.5: Augment với STEP chunks còn thiếu của top procedure(s).
        # Steps_text dài bị split thành ~6 sub-chunks, retrieval chỉ trả về
        # top-k → LLM thấy thiếu Bước 3, Bước 4. Fetch nốt các phần còn lại
        # từ DB để câu trả lời có đầy đủ trình tự.
        chunks = await self._augment_step_chunks(chunks)

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

    # ── Helpers ───────────────────────────────────────────────────────────────

    _MAX_AUGMENT_PROCEDURES = 2     # chỉ augment top 1-2 procedure để không phình context

    async def _augment_step_chunks(
        self, chunks: list[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        """
        Sau retrieval, với mỗi procedure xuất hiện ở vị trí top, fetch TẤT CẢ
        is_current=True STEP chunks của procedure đó và inject vào context
        theo đúng chunk_index. Đảm bảo LLM thấy đầy đủ Bước 1→N, không bị
        cụt vì chunker split steps_text dài.

        Giữ nguyên thứ tự ưu tiên: chunks score cao vẫn đứng trước, STEP
        chunks bổ sung (cùng procedure, chưa có trong context) chen vào cuối.
        """
        if not chunks:
            return chunks

        # Top procedure_codes — chỉ augment 1-2 cái để tránh phình context
        seen_codes: list[str] = []
        for c in chunks:
            code = c.metadata.get("procedure_code")
            if code and code not in seen_codes:
                seen_codes.append(code)
                if len(seen_codes) >= self._MAX_AUGMENT_PROCEDURES:
                    break
        if not seen_codes:
            return chunks

        # Đã có chunk_index nào của STEP rồi → không fetch lại
        existing_step_keys: set[tuple[str, int]] = set()
        for c in chunks:
            ctype = (c.metadata.get("chunk_type") or "").lower()
            if ctype in ("step", "steps"):
                code = c.metadata.get("procedure_code") or ""
                idx = c.metadata.get("chunk_index")
                if isinstance(idx, int):
                    existing_step_keys.add((code, idx))

        try:
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.procedure_code.in_(seen_codes),
                        DocumentChunk.chunk_type == ChunkType.STEP,
                        DocumentChunk.is_current.is_(True),
                    ).order_by(
                        DocumentChunk.procedure_code,
                        DocumentChunk.chunk_index,
                    )
                )).scalars().all()
        except Exception as e:
            logger.warning(f"RAG | augment_step failed | {e}")
            return chunks

        # Build score-as-attached cho chunks bổ sung — dùng score top của
        # procedure đó trong retrieval, để generator coi chúng quan trọng.
        top_score_by_code: dict[str, float] = {}
        for c in chunks:
            code = c.metadata.get("procedure_code")
            if not code:
                continue
            if code not in top_score_by_code or c.score > top_score_by_code[code]:
                top_score_by_code[code] = c.score

        added: list[RetrievedChunk] = []
        for row in rows:
            key = (row.procedure_code or "", row.chunk_index)
            if key in existing_step_keys:
                continue
            added.append(RetrievedChunk(
                vector_id=row.vector_id or row.id,
                content=row.content,
                score=top_score_by_code.get(row.procedure_code or "", 0.5),
                metadata={
                    "procedure_code": row.procedure_code,
                    "procedure_name": next(
                        (c.metadata.get("procedure_name") for c in chunks
                         if c.metadata.get("procedure_code") == row.procedure_code),
                        "",
                    ),
                    "chunk_type": "step",
                    "chunk_index": row.chunk_index,
                    "section": row.section,
                    "domain": row.domain,
                    "augmented": True,  # đánh dấu để debug
                },
            ))

        if not added:
            return chunks

        logger.info(
            f"RAG | augment_step | procs={seen_codes} | added={len(added)} step chunks"
        )
        return chunks + added
