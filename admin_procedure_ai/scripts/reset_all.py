"""
WIPE TOÀN BỘ dữ liệu crawl + chunk + vector. Clean slate.

Xóa:
  - Qdrant collection (recreate empty)
  - rag_retrievals (FK → chunks)
  - document_chunks
  - procedure_localities / procedure_fees / procedure_requirements / procedure_steps
  - procedures
  - document_sources

Giữ:
  - users, chat_sessions, conversations (set feedback.procedure_id = NULL trước
    khi xoá procedures để không vỡ FK)

Sau khi chạy → DB sạch hoàn toàn. Admin tạo source mới qua UI và crawl lại.

Usage (từ thư mục admin_procedure_ai):
    python -m scripts.reset_all              # dry-run, in số rows hiện tại
    python -m scripts.reset_all --confirm    # thực sự wipe
"""
from __future__ import annotations

import argparse
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from loguru import logger
from sqlalchemy import delete, func, select, update

from app.core.config import settings
from app.db.base import AsyncSessionLocal, engine
from app.models.conversation import RAGRetrieval
from app.models.document import DocumentChunk, DocumentSource
from app.models.feedback import Feedback
from app.models.procedure import (
    Procedure,
    ProcedureFee,
    ProcedureLocality,
    ProcedureRequirement,
    ProcedureStep,
)


TABLES_TO_COUNT = [
    ("procedures", Procedure),
    ("procedure_requirements", ProcedureRequirement),
    ("procedure_steps", ProcedureStep),
    ("procedure_fees", ProcedureFee),
    ("procedure_localities", ProcedureLocality),
    ("document_chunks", DocumentChunk),
    ("document_sources", DocumentSource),
    ("rag_retrievals", RAGRetrieval),
]


async def _count_state() -> None:
    async with AsyncSessionLocal() as db:
        for name, model in TABLES_TO_COUNT:
            n = (await db.execute(select(func.count()).select_from(model))).scalar() or 0
            logger.info(f"  {name:30s} {n:>6d} rows")


async def _wipe_db() -> None:
    async with AsyncSessionLocal() as db:
        # 1. rag_retrievals (FK → chunks)
        r = await db.execute(delete(RAGRetrieval))
        logger.info(f"  deleted rag_retrievals: {r.rowcount or 0}")

        # 2. Null out FK procedure_id ở các bảng giữ lại (feedback, document_sources)
        r = await db.execute(update(Feedback).values(procedure_id=None))
        logger.info(f"  nulled feedback.procedure_id: {r.rowcount or 0}")
        r = await db.execute(update(DocumentSource).values(procedure_id=None))
        logger.info(f"  nulled document_sources.procedure_id: {r.rowcount or 0}")

        # 3. document_chunks (FK source_id, procedure_id — both will be gone)
        r = await db.execute(delete(DocumentChunk))
        logger.info(f"  deleted document_chunks: {r.rowcount or 0}")

        # 4. Procedure children
        for model, label in [
            (ProcedureLocality, "procedure_localities"),
            (ProcedureFee, "procedure_fees"),
            (ProcedureRequirement, "procedure_requirements"),
            (ProcedureStep, "procedure_steps"),
        ]:
            r = await db.execute(delete(model))
            logger.info(f"  deleted {label}: {r.rowcount or 0}")

        # 5. Null self-FK parent_id rồi xoá procedures
        r = await db.execute(update(Procedure).values(parent_id=None))
        logger.info(f"  nulled procedures.parent_id: {r.rowcount or 0}")
        r = await db.execute(delete(Procedure))
        logger.info(f"  deleted procedures: {r.rowcount or 0}")

        # 6. document_sources cuối cùng
        r = await db.execute(delete(DocumentSource))
        logger.info(f"  deleted document_sources: {r.rowcount or 0}")

        await db.commit()


def _reset_qdrant() -> None:
    from qdrant_client.models import Distance, VectorParams

    from app.rag.embedding.embedder import _get_qdrant_client

    client = _get_qdrant_client()
    name = settings.QDRANT_COLLECTION_NAME
    existing = {c.name for c in client.get_collections().collections}
    if name in existing:
        client.delete_collection(name)
        logger.info(f"  deleted Qdrant collection '{name}'")
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(
            size=settings.EMBEDDING_DIMENSIONS, distance=Distance.COSINE
        ),
    )
    logger.info(
        f"  recreated empty collection '{name}' (dims={settings.EMBEDDING_DIMENSIONS})"
    )


async def main(confirm: bool) -> None:
    try:
        logger.info("=== CURRENT STATE ===")
        await _count_state()
        logger.info(f"Qdrant target | collection={settings.QDRANT_COLLECTION_NAME}")

        if not confirm:
            logger.warning("Dry-run. Chạy lại với --confirm để thực sự wipe.")
            return

        logger.warning("=== EXECUTING FULL WIPE ===")
        await _wipe_db()
        _reset_qdrant()

        logger.info("=== FINAL STATE ===")
        await _count_state()
        logger.info("Done. Bước tiếp: vào /admin/sources, click 1 cơ quan để crawl lại.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wipe sạch toàn bộ dữ liệu crawl")
    parser.add_argument("--confirm", action="store_true", help="Thực sự wipe (default dry-run)")
    args = parser.parse_args()
    asyncio.run(main(args.confirm))
