# app/api/v1/endpoints/admin/sources.py
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, require_admin
from app.models.document import CrawlFrequency, DocumentChunk, DocumentSource
from app.models.procedure import Procedure
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
    SourceProceduresResponse,
    SourceProcedureItem,
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
async def list_agencies(
    level: str = Query(
        "MINISTRY",
        description="Cấp cơ quan: MINISTRY (Bộ/TW), PROVINCE (UBND tỉnh), all",
    ),
    _: User = Depends(require_admin),
):
    """
    Lấy danh sách cơ quan (theo cấp) từ endpoint chính thức:
    `/api/v1/configuring/citizen/department/list-with-location`.

    Mặc định chỉ trả MINISTRY (Bộ + cơ quan TW). Pass `level=PROVINCE` để
    crawl UBND tỉnh. `code` trong response chính là `departmentCode` dùng
    để filter server-side khi crawl.
    """
    from app.crawler.sources.dvcqg_json import _warmup, fetch_agency_list

    lvl = (level or "").strip().upper()
    if lvl in ("", "ALL"):
        levels = None
    else:
        levels = [lvl]

    async with httpx.AsyncClient(
        http2=False, follow_redirects=True, timeout=60
    ) as client:
        await _warmup(client)
        agencies = await fetch_agency_list(client, levels=levels)

    if not agencies:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Không lấy được danh sách cơ quan từ Cổng DVCQG.",
        )
    return [
        AgencyItem(
            id=a["id"], name=a["name"], code=a["code"], level=a.get("level")
        )
        for a in agencies
    ]


@router.post("/crawl-agency", response_model=CrawlTriggerResponse)
async def crawl_agency(
    payload: CrawlAgencyRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Crawl toàn bộ thủ tục của 1 bộ/ngành.

    `agency_code` = `departmentCode` của API DVCQG (vd "G19" cho Ngân hàng Nhà
    nước). Crawler dùng code này để filter server-side trong list-all → tiết
    kiệm rất nhiều API request so với derive từ list-all rồi lọc client.

    Lưu source_url = agency_code (ngắn, server thật sự dùng). Dedupe trên code.
    """
    code = payload.agency_code.strip()
    # Idempotent: dedupe theo source_url = agency_code
    existing = (await db.execute(
        select(DocumentSource).where(DocumentSource.source_url == code)
    )).scalar_one_or_none()

    if existing:
        source = existing
        if payload.agency_name:
            source.title = payload.agency_name[:300]
        source.is_active = True
    else:
        source = DocumentSource(
            title=(payload.agency_name or f"Cơ quan {code}")[:300],
            source_url=code,
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
        message=f"Đã kích hoạt crawl bộ/ngành '{source.title}' (code={code}).",
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


@router.get("/{source_id}/procedures", response_model=SourceProceduresResponse)
async def list_source_procedures(
    source_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Drill-down: liệt kê các thủ tục thuộc 1 source (đã crawl + còn chunks
    is_current=True). Sort theo procedure.updated_at desc.
    """
    # Subquery: đếm chunks hiện tại per procedure trong source này
    chunks_sub = (
        select(
            DocumentChunk.procedure_id.label("pid"),
            func.count(DocumentChunk.id).label("chunks"),
        )
        .where(
            DocumentChunk.source_id == source_id,
            DocumentChunk.is_current.is_(True),
            DocumentChunk.procedure_id.is_not(None),
        )
        .group_by(DocumentChunk.procedure_id)
        .subquery()
    )

    total = (await db.execute(
        select(func.count()).select_from(chunks_sub)
    )).scalar() or 0

    rows = (await db.execute(
        select(Procedure, chunks_sub.c.chunks)
        .join(chunks_sub, Procedure.id == chunks_sub.c.pid)
        .order_by(Procedure.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()

    items = [
        SourceProcedureItem(
            code=p.code,
            name=p.name,
            domain=p.domain,
            chunk_count=int(cc),
            updated_at=p.updated_at,
        )
        for p, cc in rows
    ]
    return SourceProceduresResponse(items=items, total=total, page=page, page_size=page_size)


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
