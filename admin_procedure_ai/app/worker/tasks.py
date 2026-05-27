# app/worker/tasks.py
"""
Celery tasks cho crawl + embed thủ tục hành chính.

Flow mới (xlsx-based):
  DocumentSource.source_url quy định phạm vi crawl:
    - "" hoặc "all"  → tất cả file .xlsx trong settings.XLSX_DATA_DIR
    - "<tên>.xlsx"   → chỉ file đó (path tương đối trong XLSX_DATA_DIR)

  Pipeline mỗi task run:
    1. Đọc xlsx → list mã TTHC (cột "Mã TTHC")
    2. Với mỗi mã: rest.jsp lookup idTTHC → tải .docx → parse
    3. Upsert vào DB (Procedure + Fees + Requirements + Steps)
    4. Chunking + embed sang Qdrant
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from app.worker.celery_app import celery_app


def _run_async(coro):
    """
    Run an async coroutine from a sync Celery task.

    Mỗi task tạo event loop riêng. Trước khi đóng loop, dispose engine
    để xóa sạch connection pool — tránh lỗi 'Event loop is closed' ở task kế tiếp
    khi pool cố reuse connection từ loop cũ.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            from app.db.base import engine
            loop.run_until_complete(engine.dispose())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


@celery_app.task(
    name="app.worker.tasks.crawl_and_embed_procedure",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def crawl_and_embed_procedure(self, source_id: str) -> dict:
    """
    Crawl danh sách mã TTHC từ xlsx → tải docx → parse → embed.
    """
    return _run_async(_crawl_and_embed_async(self, source_id))


async def _crawl_and_embed_async(task, source_id: str) -> dict:
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.base import AsyncSessionLocal
    from app.models.document import CrawlStatus, ProcessingStatus, DocumentSource
    from app.crawler.sources.dvcqg_xlsx import (
        collect_all_codes,
        read_codes_from_xlsx,
        fetch_procedures,
    )

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(DocumentSource).where(DocumentSource.id == source_id))
        source: DocumentSource | None = result.scalar_one_or_none()

        if not source or not source.is_active:
            logger.warning(f"Task | crawl | source not found or inactive | source_id={source_id}")
            return {"status": "skipped", "reason": "source_not_found"}

        source.crawl_status = CrawlStatus.CRAWLING
        await db.commit()

        try:
            # ── Xác định danh sách mã TTHC cần crawl ──────────────────────────
            scope = (source.source_url or "").strip().lower()
            data_dir = Path(settings.XLSX_DATA_DIR)

            if scope in ("", "all"):
                code_metas = collect_all_codes(data_dir)
                logger.info(
                    f"Task | xlsx scope=ALL | dir={data_dir} | codes={len(code_metas)}"
                )
            else:
                xlsx_path = data_dir / source.source_url
                if not xlsx_path.exists():
                    raise FileNotFoundError(f"xlsx file not found: {xlsx_path}")
                code_metas = read_codes_from_xlsx(xlsx_path)
                for m in code_metas:
                    m["source_xlsx"] = xlsx_path.name
                logger.info(
                    f"Task | xlsx scope=FILE | file={xlsx_path.name} | codes={len(code_metas)}"
                )

            if not code_metas:
                raise ValueError(f"Không có mã TTHC nào từ scope='{scope or 'all'}'")

            # ── Fetch + parse + save từng procedure ───────────────────────────
            total_chunks = 0
            ok = 0
            failed = 0
            concurrency = settings.XLSX_CRAWL_CONCURRENCY

            async for code, parsed in fetch_procedures(code_metas, concurrency=concurrency):
                if not parsed:
                    failed += 1
                    continue
                try:
                    n = await _process_parsed_procedure(
                        source_id=source_id,
                        parsed=parsed,
                    )
                    total_chunks += n
                    ok += 1
                except Exception as e:
                    logger.warning(f"Task | persist failed | code={code} | {e}")
                    failed += 1

            # ── Cập nhật trạng thái source ────────────────────────────────────
            source.last_crawled_at = datetime.now(timezone.utc)
            source.crawl_status = CrawlStatus.SUCCESS
            source.processing_status = ProcessingStatus.EMBEDDED
            source.error_message = None
            await db.commit()

            logger.info(
                f"Task | crawl | done | source_id={source_id} "
                f"| codes={len(code_metas)} | ok={ok} | failed={failed} | chunks={total_chunks}"
            )
            return {
                "status": "success",
                "codes": len(code_metas),
                "ok": ok,
                "failed": failed,
                "chunks": total_chunks,
            }

        except Exception as exc:
            source.crawl_status = CrawlStatus.FAILED
            source.error_message = str(exc)[:1000]
            await db.commit()
            logger.error(f"Task | crawl | failed | source_id={source_id} | error={exc}")
            raise task.retry(exc=exc)


async def _process_parsed_procedure(source_id: str, parsed: dict) -> int:
    """
    Nhận parsed dict từ dvcqg_docx_parser → upsert DB + chunk + embed.
    Dùng session riêng + commit ngay → không bị ảnh hưởng bởi procedure khác.
    Trả về số chunks đã tạo.
    """
    import hashlib
    from sqlalchemy import delete, select

    from app.core.config import settings
    from app.db.base import AsyncSessionLocal
    from app.models.document import (
        ChunkType, DocumentChunk, DocumentSource,
        EmbeddingStatus, ProcessingStatus,
    )
    from app.models.procedure import (
        AuthorityLevel, Procedure, ProcedureFee, ProcedureRequirement,
        ProcedureStatus, ProcedureStep,
    )
    from app.rag.chunking.strategy import ProcedureChunker
    from app.rag.embedding.embedder import Embedder

    async with AsyncSessionLocal() as db:
        try:
            # ── Upsert Procedure record ───────────────────────────────────────
            code = parsed.get("code")
            if not code:
                logger.warning("Task | missing code in parsed dict, skip")
                return 0

            existing = (await db.execute(
                select(Procedure).where(Procedure.code == code)
            )).scalar_one_or_none()

            # Truncate fields có giới hạn VARCHAR
            raw_name = parsed.get("name") or "Không rõ"
            safe_name = raw_name[:255]
            safe_agency = (parsed.get("implementing_agency") or "")[:255] or None
            safe_domain = (parsed.get("domain") or "")[:100] or None
            fee_summary = (parsed.get("fee_summary") or "")[:500] or None
            proc_time = (parsed.get("processing_time") or "")[:200] or None

            if existing:
                procedure = existing
                procedure.name = safe_name
                procedure.domain = safe_domain
                procedure.description = parsed.get("description")
                procedure.implementing_agency = safe_agency
                procedure.authority = safe_agency
                procedure.processing_time = proc_time
                procedure.fee = fee_summary
                procedure.result = parsed.get("result")
                procedure.legal_basis = parsed.get("legal_basis")
                procedure.status = ProcedureStatus.ACTIVE
            else:
                procedure = Procedure(
                    code=code,
                    name=safe_name,
                    domain=safe_domain,
                    implementing_agency=safe_agency,
                    authority=safe_agency,
                    processing_time=proc_time,
                    fee=fee_summary,
                    result=parsed.get("result"),
                    legal_basis=parsed.get("legal_basis"),
                    authority_level=AuthorityLevel.CENTRAL,
                    status=ProcedureStatus.ACTIVE,
                )
                db.add(procedure)

            await db.flush()  # lấy procedure.id

            # ── Xóa requirements + steps + fees cũ trước khi insert mới ──────
            await db.execute(
                delete(ProcedureRequirement).where(ProcedureRequirement.procedure_id == procedure.id)
            )
            await db.execute(
                delete(ProcedureStep).where(ProcedureStep.procedure_id == procedure.id)
            )
            await db.execute(
                delete(ProcedureFee).where(ProcedureFee.procedure_id == procedure.id)
            )

            # ── Lưu fees mới ─────────────────────────────────────────────────
            for f in parsed.get("fees", []) or []:
                db.add(ProcedureFee(
                    procedure_id=procedure.id,
                    submission_method=(f.get("submission_method") or "")[:100],
                    processing_time=(f.get("processing_time") or "")[:200] or None,
                    amount_text=(f.get("amount_text") or "")[:300] or None,
                    description=f.get("description"),
                    order=f.get("order", 0),
                ))

            # ── Lưu requirements mới ─────────────────────────────────────────
            for i, req in enumerate(parsed.get("requirements", []) or []):
                db.add(ProcedureRequirement(
                    procedure_id=procedure.id,
                    name=(req.get("name") or "")[:255],
                    description=req.get("description"),
                    is_mandatory=req.get("is_mandatory", True),
                    order=i,
                    form_name=(req.get("form_name") or "")[:300] or None,
                    form_url=req.get("form_url"),
                    quantity=(req.get("quantity") or "")[:100] or None,
                    case_group=(req.get("case_group") or "")[:500] or None,
                ))

            # ── Lưu step blob (1 row/procedure theo yêu cầu) ────────────────
            steps_text = parsed.get("steps_text") or ""
            if steps_text:
                db.add(ProcedureStep(
                    procedure_id=procedure.id,
                    step_order=1,
                    title="Trình tự thực hiện",
                    description=steps_text,
                ))

            # ── Change detection cho chunks ──────────────────────────────────
            content_hash = hashlib.sha256(
                (steps_text + (parsed.get("fee_summary") or "") + str(parsed.get("requirements", "")))
                .encode("utf-8")
            ).hexdigest()

            source_result = await db.execute(
                select(DocumentSource).where(DocumentSource.id == source_id)
            )
            source = source_result.scalar_one()

            # Mark old chunks of this procedure as stale + delete vectors
            old_chunks_result = await db.execute(
                select(DocumentChunk).where(
                    DocumentChunk.source_id == source_id,
                    DocumentChunk.procedure_id == procedure.id,
                    DocumentChunk.is_current == True,  # noqa: E712
                )
            )
            old_chunks = old_chunks_result.scalars().all()
            old_vector_ids = [c.vector_id for c in old_chunks if c.vector_id]

            embedder = Embedder()
            if old_vector_ids:
                try:
                    embedder.delete_by_ids(old_vector_ids)
                except Exception as e:
                    logger.warning(f"Task | delete old vectors failed | {e}")

            for c in old_chunks:
                c.is_current = False

            # ── Chunk + embed ─────────────────────────────────────────────────
            # Inject id để chunking ghi vào payload Qdrant (cho debug/filter)
            parsed["id"] = procedure.id
            parsed["authority_level"] = AuthorityLevel.CENTRAL.value

            chunker = ProcedureChunker()
            chunks = chunker.chunk_procedure(parsed)

            if not chunks:
                logger.warning(f"Task | no chunks generated | code={code}")
                await db.commit()
                return 0

            embedded = embedder.embed_chunks(chunks, source_id)

            for idx, item in enumerate(embedded):
                db.add(DocumentChunk(
                    source_id=source_id,
                    procedure_id=procedure.id,
                    vector_id=item["vector_id"],
                    content=item["content"],
                    chunk_index=idx,
                    chunk_type=item.get("chunk_type", ChunkType.GENERAL),
                    procedure_code=code,
                    domain=safe_domain,
                    authority_level=AuthorityLevel.CENTRAL.value,
                    locality=item["metadata"].get("locality"),
                    section=(item["metadata"].get("section") or "")[:200],
                    step_order=item["metadata"].get("step_order"),
                    is_current=True,
                    embedding_model=settings.EMBEDDING_MODEL,
                    embedding_status=EmbeddingStatus.DONE,
                ))

            source.content_hash = content_hash
            source.processing_status = ProcessingStatus.EMBEDDED

            # ── Commit ngay cho procedure này ────────────────────────────────
            await db.commit()

            logger.info(f"Task | procedure done | code={code} | chunks={len(embedded)}")
            return len(embedded)

        except Exception as exc:
            await db.rollback()
            raise


@celery_app.task(name="app.worker.tasks.scheduled_crawl")
def scheduled_crawl() -> dict:
    """Nightly job: crawl all active sources."""
    return _run_async(_scheduled_crawl_async())


async def _scheduled_crawl_async() -> dict:
    from sqlalchemy import select

    from app.db.base import AsyncSessionLocal
    from app.models.document import DocumentSource

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DocumentSource).where(DocumentSource.is_active == True)  # noqa: E712
        )
        sources = result.scalars().all()

    triggered = 0
    for source in sources:
        crawl_and_embed_procedure.delay(source.id)
        triggered += 1

    logger.info(f"Task | scheduled_crawl | triggered={triggered} sources")
    return {"triggered": triggered}


@celery_app.task(name="app.worker.tasks.retry_failed_embeddings")
def retry_failed_embeddings() -> dict:
    """Hourly: retry any chunks that failed embedding."""
    return _run_async(_retry_failed_async())


async def _retry_failed_async() -> dict:
    from sqlalchemy import select

    from app.db.base import AsyncSessionLocal
    from app.models.document import CrawlStatus, DocumentSource

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DocumentSource).where(DocumentSource.crawl_status == CrawlStatus.FAILED)
        )
        failed_sources = result.scalars().all()

    retried = 0
    for source in failed_sources:
        crawl_and_embed_procedure.delay(source.id)
        retried += 1

    logger.info(f"Task | retry_failed | retried={retried}")
    return {"retried": retried}
