"""
Reset toàn bộ embeddings để re-index với model mới (Gemini gemini-embedding-001 / 3072 dims).

Tác vụ:
1. Drop Qdrant collection cũ (vector dims không tương thích)
2. Xóa toàn bộ rows trong document_chunks
3. Reset document_sources: processing_status → PENDING, content_hash → NULL,
   crawl_status → PENDING (để trigger lại crawl pipeline đầy đủ)

Sau khi chạy xong → trigger re-crawl qua admin API hoặc gọi trực tiếp celery task.

Usage (từ thư mục admin_procedure_ai):
    python -m scripts.reset_embeddings                          # dry-run, chỉ in số lượng
    python -m scripts.reset_embeddings --confirm                # thực sự xóa
    python -m scripts.reset_embeddings --confirm --retrigger    # xóa + enqueue crawl cho all sources active
"""
import argparse
import asyncio
import sys

from loguru import logger
from sqlalchemy import delete, select, update

from app.core.config import settings
from app.db.base import AsyncSessionLocal
from app.models.conversation import RAGRetrieval
from app.models.document import (
    CrawlStatus,
    DocumentChunk,
    DocumentSource,
    ProcessingStatus,
)


async def _count_state() -> tuple[int, int]:
    async with AsyncSessionLocal() as db:
        chunks_count = (await db.execute(
            select(DocumentChunk.id)
        )).scalars().all()
        sources_count = (await db.execute(
            select(DocumentSource.id)
        )).scalars().all()
        return len(chunks_count), len(sources_count)


async def _reset_db() -> None:
    async with AsyncSessionLocal() as db:
        # 0. Xóa rag_retrievals trước — có FK chunk_id → document_chunks.id
        #    Dữ liệu này là audit log retrieval cũ, xóa để bỏ FK constraint.
        result = await db.execute(delete(RAGRetrieval))
        deleted_retrievals = result.rowcount or 0
        logger.info(f"DB | deleted {deleted_retrievals} rows from rag_retrievals")

        # 1. Xóa toàn bộ document_chunks
        result = await db.execute(delete(DocumentChunk))
        deleted_chunks = result.rowcount or 0
        logger.info(f"DB | deleted {deleted_chunks} rows from document_chunks")

        # 2. Reset document_sources về trạng thái pending để re-crawl
        result = await db.execute(
            update(DocumentSource)
            .values(
                processing_status=ProcessingStatus.PENDING,
                crawl_status=CrawlStatus.PENDING,
                content_hash=None,
                change_detected=False,
                error_message=None,
            )
        )
        reset_sources = result.rowcount or 0
        logger.info(f"DB | reset {reset_sources} rows in document_sources")

        await db.commit()


def _drop_qdrant_collection() -> None:
    # Import lazy để singleton không tự tạo collection trước khi xóa
    from app.rag.embedding.embedder import _get_qdrant_client

    # Lưu ý: _get_qdrant_client() sẽ tự gọi _ensure_collection().
    # Nếu collection cũ vẫn 1024d → singleton vẫn dùng nó. Cần delete rồi mới tạo lại.
    client = _get_qdrant_client()
    name = settings.QDRANT_COLLECTION_NAME

    existing = {c.name for c in client.get_collections().collections}
    if name in existing:
        client.delete_collection(name)
        logger.info(f"Qdrant | deleted collection '{name}'")
    else:
        logger.info(f"Qdrant | collection '{name}' does not exist, skipping")

    # Tạo lại với dims mới (đọc từ settings)
    from qdrant_client.models import Distance, VectorParams
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(
            size=settings.EMBEDDING_DIMENSIONS,
            distance=Distance.COSINE,
        ),
    )
    logger.info(
        f"Qdrant | recreated collection '{name}' | dims={settings.EMBEDDING_DIMENSIONS}"
    )


async def _enqueue_crawl_tasks() -> int:
    from app.worker.tasks import crawl_and_embed_procedure

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DocumentSource.id).where(DocumentSource.is_active.is_(True))
        )
        source_ids = result.scalars().all()

    for sid in source_ids:
        task = crawl_and_embed_procedure.delay(sid)
        logger.info(f"Celery | enqueued | source_id={sid} | task_id={task.id}")
    return len(source_ids)


async def main(confirm: bool, retrigger: bool) -> None:
    chunks, sources = await _count_state()
    logger.info(f"Current state | document_chunks={chunks} | document_sources={sources}")
    logger.info(f"Target | model={settings.EMBEDDING_MODEL} | dims={settings.EMBEDDING_DIMENSIONS}")

    if not confirm:
        logger.warning("Dry-run mode. Chạy lại với --confirm để thực sự xóa.")
        return

    logger.info("=== EXECUTING RESET ===")
    await _reset_db()
    _drop_qdrant_collection()
    logger.info("=== RESET DONE ===")

    if retrigger:
        logger.info("=== ENQUEUEING CRAWL TASKS ===")
        n = await _enqueue_crawl_tasks()
        logger.info(f"Celery | enqueued {n} sources. Đảm bảo celery worker đang chạy.")
    else:
        logger.info(
            "Bước tiếp theo: trigger lại crawl qua POST /api/v1/admin/sources/trigger-crawl "
            "hoặc chạy lại script với --retrigger."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset embeddings để re-index với model mới")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Thực sự xóa (mặc định dry-run)",
    )
    parser.add_argument(
        "--retrigger",
        action="store_true",
        help="Sau khi reset, enqueue celery crawl task cho tất cả sources is_active=True",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(args.confirm, args.retrigger))
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        sys.exit(1)
