# app/api/v1/endpoints/admin/sources.py
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, require_admin
from app.models.document import CrawlFrequency, DocumentSource
from app.models.user import User
from app.schemas.admin import (
    AgencyItem,
    CrawlAgencyRequest,
    CrawlByCodeResponse,
    CrawlProcedureRequest,
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


# ── Danh sách bộ/ngành (lấy động từ API DVCQG, không dùng file local) ──────────

@router.get("/agencies", response_model=list[AgencyItem])
async def list_agencies(_: User = Depends(require_admin)):
    """
    Lấy danh sách cơ quan (bộ/ngành) trực tiếp từ Cổng DVCQG để admin chọn crawl.
    """
    from app.crawler.sources.dvcqg_xlsx import fetch_agency_list

    async with httpx.AsyncClient(follow_redirects=True) as client:
        agencies = await fetch_agency_list(client)

    if not agencies:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Không lấy được danh sách cơ quan từ Cổng DVCQG.",
        )
    return [AgencyItem(id=a["id"], name=a["name"], code=a.get("code")) for a in agencies]


@router.post("/crawl-agency", response_model=CrawlTriggerResponse)
async def crawl_agency(
    payload: CrawlAgencyRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Crawl toàn bộ thủ tục của 1 bộ/ngành.
    Tạo (hoặc tái sử dụng) 1 DocumentSource với source_url = agency_id rồi enqueue task.
    """
    # Idempotent: dedupe theo source_url = agency_id
    existing = (await db.execute(
        select(DocumentSource).where(DocumentSource.source_url == payload.agency_id)
    )).scalar_one_or_none()

    if existing:
        source = existing
        if payload.agency_name:
            source.title = payload.agency_name[:300]
        source.is_active = True
    else:
        source = DocumentSource(
            title=(payload.agency_name or f"Cơ quan {payload.agency_id}")[:300],
            source_url=payload.agency_id,
            source_type="dvcqg_agency",
            is_active=True,
            crawl_frequency=CrawlFrequency.MANUAL,
        )
        db.add(source)
    await db.flush()
    await db.refresh(source)

    from app.worker.tasks import crawl_and_embed_procedure
    task = crawl_and_embed_procedure.delay(source.id)
    return CrawlTriggerResponse(
        task_id=task.id,
        source_id=source.id,
        message=f"Đã kích hoạt crawl bộ/ngành '{source.title}'.",
    )


@router.post("/crawl-procedure", response_model=CrawlByCodeResponse)
async def crawl_procedure(
    payload: CrawlProcedureRequest,
    _: User = Depends(require_admin),
):
    """Crawl 1 thủ tục lẻ theo mã TTHC (vd '1.015028')."""
    from app.worker.tasks import crawl_single_procedure
    task = crawl_single_procedure.delay(payload.code)
    return CrawlByCodeResponse(
        task_id=task.id,
        code=payload.code,
        message=f"Đã kích hoạt crawl thủ tục {payload.code}.",
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
