# app/rag/pipeline.py
import time
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.db.base import AsyncSessionLocal
from app.models.document import ChunkType, DocumentChunk
from app.models.procedure import Procedure, ProcedureStep
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

    _MAX_AUGMENT_PROCEDURES = 1     # chỉ augment TOP 1: nhồi 2 procedure full → blow max_tokens

    async def _augment_step_chunks(
        self, chunks: list[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        """
        Sau retrieval, với 1-2 procedure xuất hiện ở top, lấy TRỌN
        `ProcedureStep.description` (full text 2-3k ký tự, chưa bị chunker
        split) inject thành 1 chunk lớn. Đảm bảo LLM thấy đầy đủ Bước 1→N
        trong 1 khối liền mạch, không phải 6 mảnh "phần 1/6, phần 2/6, ..."
        rời rạc khiến LLM dễ rút gọn.

        Strategy: REPLACE — xoá hết STEP chunks fragmented của procedure
        được augment, thay bằng 1 chunk lớn. Tránh duplicate + LLM nhầm
        lẫn các phần.
        """
        if not chunks:
            return chunks

        # Top procedure_codes — chỉ augment 1-2 cái để không phình context
        seen_codes: list[str] = []
        for c in chunks:
            code = c.metadata.get("procedure_code")
            if code and code not in seen_codes:
                seen_codes.append(code)
                if len(seen_codes) >= self._MAX_AUGMENT_PROCEDURES:
                    break
        if not seen_codes:
            return chunks

        # Top score per procedure để gán cho mega-chunk
        top_score_by_code: dict[str, float] = {}
        name_by_code: dict[str, str] = {}
        domain_by_code: dict[str, str] = {}
        for c in chunks:
            code = c.metadata.get("procedure_code")
            if not code:
                continue
            if code not in top_score_by_code or c.score > top_score_by_code[code]:
                top_score_by_code[code] = c.score
            if code not in name_by_code:
                name_by_code[code] = c.metadata.get("procedure_name") or ""
                domain_by_code[code] = c.metadata.get("domain") or ""

        try:
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(Procedure.code, Procedure.name, ProcedureStep.description)
                    .join(ProcedureStep, ProcedureStep.procedure_id == Procedure.id)
                    .where(Procedure.code.in_(seen_codes))
                )).all()
        except Exception as e:
            logger.warning(f"RAG | augment_step failed | {e}")
            return chunks

        if not rows:
            return chunks

        # Loại STEP chunks fragmented của procedures được augment
        replaced_codes = {code for code, _, _ in rows}
        kept: list[RetrievedChunk] = []
        dropped = 0
        for c in chunks:
            ctype = (c.metadata.get("chunk_type") or "").lower()
            code = c.metadata.get("procedure_code") or ""
            if ctype in ("step", "steps") and code in replaced_codes:
                dropped += 1
                continue
            kept.append(c)

        # Thêm mega-chunk: 1 chunk/procedure chứa full steps_text
        added = 0
        for code, name, desc in rows:
            if not desc or not desc.strip():
                continue
            content = (
                f"Thủ tục: {name or name_by_code.get(code, '')}\n"
                f"Trình tự thực hiện (FULL — KHÔNG được rút gọn):\n{desc.strip()}"
            )
            kept.append(RetrievedChunk(
                vector_id=f"augment:step:{code}",
                content=content,
                score=top_score_by_code.get(code, 0.7),
                metadata={
                    "procedure_code": code,
                    "procedure_name": name or name_by_code.get(code, ""),
                    "chunk_type": "step",
                    "section": "Trình tự thực hiện (full)",
                    "domain": domain_by_code.get(code, ""),
                    "augmented": True,
                },
            ))
            added += 1

        logger.info(
            f"RAG | augment_step | procs={seen_codes} | dropped_fragments={dropped} "
            f"| added_mega={added}"
        )
        return kept
