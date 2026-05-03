# app/api/v1/endpoints/admin/sources.py
from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, require_admin
from app.models.document import DocumentSource
from app.models.user import User
from app.schemas.admin import (
    CrawlTriggerRequest,
    CrawlTriggerResponse,
    DocumentSourceCreate,
    DocumentSourceResponse,
)
from app.schemas.common import MessageResponse

router = APIRouter(prefix="/sources", tags=["Admin - Sources"])


@router.get("", response_model=list[DocumentSourceResponse])
async def list_sources(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(DocumentSource).order_by(DocumentSource.created_at.desc()))
    return [DocumentSourceResponse.model_validate(s) for s in result.scalars().all()]


@router.post("", response_model=DocumentSourceResponse, status_code=status.HTTP_201_CREATED)
async def create_source(
    payload: DocumentSourceCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    source = DocumentSource(
        title=payload.title,
        source_url=payload.source_url,
        source_type=payload.source_type,
        crawl_frequency=payload.crawl_frequency,
    )
    db.add(source)
    await db.flush()
    await db.refresh(source)  # load server_default fields (created_at, updated_at...)
    return DocumentSourceResponse.model_validate(source)


@router.post("/trigger-crawl", response_model=CrawlTriggerResponse)
async def trigger_crawl(
    payload: CrawlTriggerRequest,
    _: User = Depends(require_admin),
):
    from app.worker.tasks import crawl_and_embed_procedure
    task = crawl_and_embed_procedure.delay(payload.source_id)
    return CrawlTriggerResponse(
        task_id=task.id,
        source_id=payload.source_id,
        message="Đã kích hoạt thu thập dữ liệu. Kiểm tra trạng thái qua task_id.",
    )


@router.delete("/{source_id}", response_model=MessageResponse)
async def deactivate_source(
    source_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(DocumentSource).where(DocumentSource.id == source_id))
    source = result.scalar_one_or_none()
    if not source:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Nguồn dữ liệu không tồn tại.")
    source.is_active = False
    return MessageResponse(message="Đã vô hiệu hóa nguồn dữ liệu.")
