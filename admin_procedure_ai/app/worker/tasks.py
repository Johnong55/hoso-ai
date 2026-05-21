# app/worker/tasks.py
import asyncio
from datetime import datetime, timezone

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
    Crawl một DocumentSource:
    - Nếu source_url là trang chủ/nhóm → discover toàn bộ procedure URLs rồi crawl từng cái
    - Nếu source_url là trang chi tiết 1 thủ tục → crawl trực tiếp
    """
    return _run_async(_crawl_and_embed_async(self, source_id))


async def _crawl_and_embed_async(task, source_id: str) -> dict:
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.base import AsyncSessionLocal
    from app.models.document import CrawlStatus, ProcessingStatus, DocumentSource
    from app.crawler.sources.dvcqg import DVCQGCrawler

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(DocumentSource).where(DocumentSource.id == source_id))
        source: DocumentSource | None = result.scalar_one_or_none()

        if not source or not source.is_active:
            logger.warning(f"Task | crawl | source not found or inactive | source_id={source_id}")
            return {"status": "skipped", "reason": "source_not_found"}

        source.crawl_status = CrawlStatus.CRAWLING
        await db.commit()

        try:
            crawler = DVCQGCrawler()
            source_url = source.source_url

            # ── Xác định luồng crawl ──────────────────────────────────────────
            # Nếu URL là trang chủ hoặc trang nhóm → discover all procedures trước
            is_homepage = (
                "dvc-trang-chu" in source_url
                or "dvc-chi-tiet-nhom" in source_url
                or source_url.rstrip("/").endswith("dvc-trang-chu.html")
            )

            if is_homepage:
                logger.info(f"Task | crawl | homepage mode | discovering procedure URLs from {source_url}")
                procedure_urls = await crawler.discover_all_procedure_urls()
                logger.info(f"Task | crawl | discovered {len(procedure_urls)} procedure URLs")
            else:
                # Crawl trực tiếp 1 thủ tục
                procedure_urls = [source_url]

            if not procedure_urls:
                raise ValueError(f"Không tìm thấy procedure URL nào từ {source_url}")

            # ── Crawl + parse + chunk + embed từng thủ tục ───────────────────
            # Batch theo nhóm 5 URL — dùng 1 browser cho cả batch, nhanh hơn nhiều
            total_chunks = 0
            failed = 0
            batch_size = 5

            for i in range(0, len(procedure_urls), batch_size):
                batch_urls = procedure_urls[i: i + batch_size]
                try:
                    parsed_list = await crawler.fetch_procedures_batch(batch_urls)
                except Exception as e:
                    logger.warning(f"Task | crawl | batch fetch failed | {e}")
                    failed += len(batch_urls)
                    continue

                for proc_url, parsed_data in zip(batch_urls, parsed_list):
                    if not parsed_data:
                        failed += 1
                        continue
                    try:
                        # Mỗi procedure dùng session riêng → commit độc lập
                        chunks_created = await _process_parsed_procedure(
                            source_id=source_id,
                            proc_url=proc_url,
                            parsed=parsed_data,
                        )
                        total_chunks += chunks_created
                    except Exception as e:
                        logger.warning(f"Task | crawl | procedure failed | url={proc_url} | {e}")
                        failed += 1

            # ── Cập nhật trạng thái source ────────────────────────────────────
            source.last_crawled_at = datetime.now(timezone.utc)
            source.crawl_status = CrawlStatus.SUCCESS
            source.processing_status = ProcessingStatus.EMBEDDED
            source.error_message = None
            await db.commit()

            logger.info(
                f"Task | crawl | done | source_id={source_id} "
                f"| procedures={len(procedure_urls)} | chunks={total_chunks} | failed={failed}"
            )
            return {
                "status": "success",
                "procedures": len(procedure_urls),
                "chunks": total_chunks,
                "failed": failed,
            }

        except Exception as exc:
            source.crawl_status = CrawlStatus.FAILED
            source.error_message = str(exc)[:1000]
            await db.commit()
            logger.error(f"Task | crawl | failed | source_id={source_id} | error={exc}")
            raise task.retry(exc=exc)


async def _process_parsed_procedure(source_id: str, proc_url: str, parsed: dict) -> int:
    """
    Nhận parsed dict → chunk → embed → lưu DB + Chroma.
    Dùng session riêng + commit ngay → không bị ảnh hưởng bởi procedure khác.
    Trả về số chunks đã tạo.
    """
    import hashlib
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.base import AsyncSessionLocal
    from app.models.document import (
        ChunkType, DocumentChunk, DocumentSource,
        EmbeddingStatus, ProcessingStatus,
    )
    from app.models.procedure import (
        AuthorityLevel, Procedure, ProcedureRequirement,
        ProcedureStatus, ProcedureStep,
    )
    from app.rag.chunking.strategy import ProcedureChunker
    from app.rag.embedding.embedder import Embedder

    async with AsyncSessionLocal() as db:
        try:
            # ── Upsert Procedure record ───────────────────────────────────────
            code = parsed.get("code") or parsed.get("name", "")[:50]
            existing = (await db.execute(
                select(Procedure).where(Procedure.code == code)
            )).scalar_one_or_none()

            # Truncate fields có giới hạn VARCHAR — parser đôi khi ghép nhiều
            # title (cách bằng ";") làm name dài >255 chars (VD: thủ tục hải quan)
            raw_name = parsed.get("name") or "Không rõ"
            safe_name = raw_name[:255]
            safe_agency = (parsed.get("implementing_agency") or "")[:255] or None
            safe_domain = (parsed.get("domain") or "")[:100] or None

            if existing:
                procedure = existing
                procedure.name = safe_name
                procedure.description = parsed.get("description")
                procedure.implementing_agency = safe_agency
                procedure.processing_time = parsed.get("processing_time")
                procedure.fee = parsed.get("fee")
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
                    processing_time=parsed.get("processing_time"),
                    fee=parsed.get("fee"),
                    result=parsed.get("result"),
                    legal_basis=parsed.get("legal_basis"),
                    authority_level=AuthorityLevel.CENTRAL,
                    status=ProcedureStatus.ACTIVE,
                )
                db.add(procedure)

            await db.flush()  # lấy procedure.id

            # ── Xóa requirements + steps cũ trước khi insert mới ─────────────
            from sqlalchemy import delete
            await db.execute(
                delete(ProcedureRequirement).where(ProcedureRequirement.procedure_id == procedure.id)
            )
            await db.execute(
                delete(ProcedureStep).where(ProcedureStep.procedure_id == procedure.id)
            )

            # ── Lưu requirements mới ─────────────────────────────────────────
            for i, req in enumerate(parsed.get("requirements", [])):
                db.add(ProcedureRequirement(
                    procedure_id=procedure.id,
                    name=req.get("name", "")[:255],
                    description=req.get("description"),
                    is_mandatory=req.get("is_mandatory", True),
                    order=i,
                    form_name=req.get("form_name"),
                    form_url=req.get("form_url"),
                    case_group=req.get("case_group"),
                ))

            # ── Lưu steps mới ────────────────────────────────────────────────
            for step in parsed.get("steps", []):
                db.add(ProcedureStep(
                    procedure_id=procedure.id,
                    step_order=step.get("order", 0),
                    title=step.get("title", "")[:255],       # "Bước 1", "Bước 2"...
                    description=step.get("description"),     # nội dung đầy đủ
                ))

            # ── Change detection cho chunks ──────────────────────────────────
            content_hash = parsed.get("content_hash") or hashlib.sha256(
                str(parsed).encode()
            ).hexdigest()

            source_result = await db.execute(
                select(DocumentSource).where(DocumentSource.id == source_id)
            )
            source = source_result.scalar_one()

            # Mark old chunks as stale
            old_chunks_result = await db.execute(
                select(DocumentChunk).where(
                    DocumentChunk.source_id == source_id,
                    DocumentChunk.procedure_id == procedure.id,
                    DocumentChunk.is_current == True,
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

            # ── Download + parse biểu mẫu ────────────────────────────────────
            from app.crawler.parsers.form_parser import FormParser
            form_parser = FormParser()
            parsed["forms"] = []

            for req in parsed.get("requirements", []):
                form_url = req.get("form_url")
                if not form_url:
                    continue
                form_data = await form_parser.parse_form(
                    form_url=form_url,
                    form_name=req.get("form_name") or req.get("name", ""),
                )
                if form_data:
                    parsed["forms"].append(form_data)
                    logger.info(
                        f"Task | form parsed | name={form_data['form_name']} "
                        f"| fields={len(form_data.get('fields', []))}"
                    )

            # ── Chunk + embed ─────────────────────────────────────────────────
            chunker = ProcedureChunker()
            chunks = chunker.chunk_procedure(parsed)

            if not chunks:
                logger.warning(f"Task | no chunks generated | url={proc_url}")
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
                    domain=parsed.get("domain"),
                    authority_level=AuthorityLevel.CENTRAL.value,
                    locality=item["metadata"].get("locality"),
                    section=(item["metadata"].get("section") or "")[:200],
                    step_order=item["metadata"].get("step_order"),
                    is_current=True,
                    embedding_model=settings.OPENAI_EMBEDDING_MODEL,
                    embedding_status=EmbeddingStatus.DONE,
                ))

            source.content_hash = content_hash
            source.processing_status = ProcessingStatus.EMBEDDED

            # ── Commit ngay cho procedure này ────────────────────────────────
            await db.commit()

            logger.info(f"Task | procedure done | code={code} | chunks={len(embedded)} | url={proc_url}")
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
            select(DocumentSource).where(DocumentSource.is_active == True)
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
