# app/api/v1/endpoints/procedures.py
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user_optional, get_db, require_admin
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


@router.get("", response_model=PaginatedResponse[ProcedureListItem])
async def search_procedures(
    q: str | None = Query(None, max_length=300),
    domain: str | None = None,
    authority_level: AuthorityLevel | None = None,
    locality: str | None = None,
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
    return await service.search(params)


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
