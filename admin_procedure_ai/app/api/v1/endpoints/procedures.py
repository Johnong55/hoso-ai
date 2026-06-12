# app/api/v1/endpoints/procedures.py
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user_optional, get_db, require_admin
from app.models.document import DocumentSource
from app.models.procedure import AuthorityLevel, ProcedureStatus
from app.models.user import User
from app.schemas.common import MessageResponse, PaginatedResponse
from app.schemas.procedure import (
    ProcedureCreateRequest,
    ProcedureDetail,
    ProcedureListItem,
    ProcedureSearchRequest,
    ProcedureUpdateRequest,
)
from app.services.procedure.procedure_service import ProcedureService

router = APIRouter(prefix="/procedures", tags=["Procedures"])


class SourceOption(BaseModel):
    """1 nguồn (bộ/ngành hoặc tỉnh/TP) đã crawl — dùng cho dropdown filter."""
    code: str         # source_url (G02, H49, ...)
    name: str         # title (Bộ Công thương, UBND tỉnh Quảng Ninh, ...)
    kind: str         # 'agency' hoặc 'province'


@router.get("/sources", response_model=list[SourceOption])
async def list_sources(db: AsyncSession = Depends(get_db)):
    """Danh sách bộ ngành + tỉnh đã được crawl. Công khai (không cần auth)
    — FE dùng để build dropdown filter trong trang thư viện thủ tục."""
    rows = (await db.execute(
        select(DocumentSource.source_url, DocumentSource.title, DocumentSource.source_type)
        .where(
            DocumentSource.source_type.in_(["dvcqg_agency", "dvcqg_province"]),
            DocumentSource.is_active == True,  # noqa: E712
        )
        .order_by(DocumentSource.source_type, DocumentSource.title)
    )).all()
    return [
        SourceOption(
            code=r[0],
            name=r[1],
            kind="agency" if r[2] == "dvcqg_agency" else "province",
        )
        for r in rows
    ]


@router.get("", response_model=PaginatedResponse[ProcedureListItem])
async def search_procedures(
    q: str | None = Query(None, max_length=300),
    domain: str | None = None,
    authority_level: AuthorityLevel | None = None,
    locality: str | None = None,
    # Phase 14.1: filter theo bộ/tỉnh — match DocumentSource.source_url
    # (G02 = Bộ Công thương, H49 = UBND tỉnh Quảng Ninh).
    agency_code: str | None = Query(None, max_length=20),
    status: ProcedureStatus | None = ProcedureStatus.ACTIVE,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(get_current_user_optional),
):
    """Tìm kiếm thủ tục hành chính. Công khai cho tất cả người dùng."""
    service = ProcedureService(db)
    params = ProcedureSearchRequest(
        q=q, domain=domain, authority_level=authority_level,
        locality=locality, status=status, page=page, page_size=page_size,
    )
    return await service.search(params, agency_code=agency_code)


@router.get("/{procedure_id}", response_model=ProcedureDetail)
async def get_procedure(
    procedure_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Lấy chi tiết một thủ tục hành chính."""
    service = ProcedureService(db)
    return await service.get_by_id(procedure_id)


@router.post("", response_model=ProcedureDetail)
async def create_procedure(
    payload: ProcedureCreateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Tạo thủ tục mới (chỉ Admin)."""
    service = ProcedureService(db)
    return await service.create(payload)


@router.put("/{procedure_id}", response_model=ProcedureDetail)
async def update_procedure(
    procedure_id: str,
    payload: ProcedureUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Cập nhật thủ tục (chỉ Admin)."""
    service = ProcedureService(db)
    return await service.update(procedure_id, payload)


@router.post("/{procedure_id}/publish", response_model=ProcedureDetail)
async def publish_procedure(
    procedure_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Phê duyệt và công bố thủ tục (chỉ Admin)."""
    service = ProcedureService(db)
    return await service.publish(procedure_id)


@router.post("/{procedure_id}/version", response_model=ProcedureDetail)
async def create_new_version(
    procedure_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Tạo phiên bản mới của thủ tục (chỉ Admin)."""
    service = ProcedureService(db)
    return await service.create_version(procedure_id)
