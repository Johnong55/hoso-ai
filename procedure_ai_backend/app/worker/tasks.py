# app/worker/tasks.py
import asyncio
from datetime import datetime, timezone

from loguru import logger

from app.worker.celery_app import celery_app


def _run_async(coro):
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(
    name="app.worker.tasks.crawl_and_embed_procedure",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def crawl_and_embed_procedure(self, source_id: str) -> dict:
    """
    Crawl a single document source, parse, chunk, and embed into Chroma.
    Implements change detection: skips re-embed if content_hash unchanged.
    """
    return _run_async(_crawl_and_embed_async(self, source_id))


async def _crawl_and_embed_async(task, source_id: str) -> dict:
    from sqlalchemy import select

    from app.db.base import AsyncSessionLocal
    from app.models.document import CrawlStatus, DocumentChunk, DocumentSource
    from app.rag.chunking.strategy import ProcedureChunker
    from app.rag.embedding.embedder import Embedder
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
            parsed = await crawler.fetch_procedure(source.url)

            if not parsed:
                raise ValueError("Parser returned no data")

            new_hash = parsed["content_hash"]

            # Change detection — skip re-embed if content unchanged
            if source.content_hash == new_hash:
                source.last_crawled_at = datetime.now(timezone.utc)
                source.crawl_status = CrawlStatus.SKIPPED
                await db.commit()
                logger.info(f"Task | crawl | unchanged | source_id={source_id}")
                return {"status": "skipped", "reason": "content_unchanged"}

            # Mark old chunks as not current and remove from Chroma
            old_chunks_result = await db.execute(
                select(DocumentChunk).where(
                    DocumentChunk.source_id == source_id,
                    DocumentChunk.is_current == True,
                )
            )
            old_chunks = old_chunks_result.scalars().all()
            old_vector_ids = [c.vector_id for c in old_chunks if c.vector_id]

            embedder = Embedder()
            if old_vector_ids:
                embedder.delete_by_ids(old_vector_ids)
            for c in old_chunks:
                c.is_current = False

            # Chunk and embed new content
            chunker = ProcedureChunker()
            chunks = chunker.chunk_procedure(parsed)
            embedded = embedder.embed_chunks(chunks, source_id)

            for item in embedded:
                db.add(DocumentChunk(
                    source_id=source_id,
                    vector_id=item["vector_id"],
                    content=item["content"],
                    chunk_index=embedded.index(item),
                    chunk_type=item["chunk_type"],
                    procedure_code=item["metadata"].get("procedure_code"),
                    domain=item["metadata"].get("domain"),
                    authority_level=item["metadata"].get("authority_level"),
                    locality=item["metadata"].get("locality"),
                    section=item["metadata"].get("section"),
                    step_order=item["metadata"].get("step_order"),
                    is_current=True,
                    embedding_model=source.name,
                ))

            source.content_hash = new_hash
            source.last_crawled_at = datetime.now(timezone.utc)
            source.crawl_status = CrawlStatus.SUCCESS
            source.error_message = None
            await db.commit()

            logger.info(f"Task | crawl | success | source_id={source_id} | chunks={len(embedded)}")
            return {"status": "success", "chunks": len(embedded)}

        except Exception as exc:
            source.crawl_status = CrawlStatus.FAILED
            source.error_message = str(exc)[:1000]
            await db.commit()
            logger.error(f"Task | crawl | failed | source_id={source_id} | error={exc}")
            raise task.retry(exc=exc)


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
