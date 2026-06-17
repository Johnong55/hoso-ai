"""Admin Procedures — quản lý + cleanup procedures.

Khác với endpoint `/procedures` (public browse), tab này hỗ trợ:
  - Lọc theo các vấn đề chất lượng dữ liệu:
      * orphan: procedure không có chunks (embedding fail / chưa chạy)
      * stale: chưa được crawl trong N ngày
      * no_steps: không có procedure_steps
      * failed_forms: có form bị parse fail / unsupported
  - Bulk re-embed nhiều procedures cùng lúc
  - Soft-delete procedures (set status=INACTIVE)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, require_admin
from app.models.document import DocumentChunk
from app.models.procedure import (
    Procedure,
    ProcedureRequirement,
    ProcedureStatus,
    ProcedureStep,
)
from app.models.user import User
from app.schemas.common import MessageResponse

router = APIRouter(prefix="/procedures", tags=["Admin - Procedures"])


# ── Schemas ─────────────────────────────────────────────────────────────────

class AdminProcedureItem(BaseModel):
    id: str
    code: str
    name: str
    domain: str | None = None
    authority: str | None = None
    status: str
    chunk_count: int = 0
    has_failed_forms: bool = False
    last_crawled_at: datetime | None = None
    updated_at: datetime


class AdminProcedureListResponse(BaseModel):
    items: list[AdminProcedureItem]
    total: int
    page: int
    page_size: int


class BulkActionRequest(BaseModel):
    codes: list[str]


class BulkActionResponse(BaseModel):
    affected: int
    failed_codes: list[str] = []


# ── List with admin filters ─────────────────────────────────────────────────

@router.get("", response_model=AdminProcedureListResponse)
async def list_procedures_admin(
    q: str = Query("", description="Tìm theo tên hoặc mã"),
    issue: Optional[str] = Query(
        None,
        description="Lọc theo vấn đề: orphan | stale | no_steps | failed_forms | inactive",
    ),
    stale_days: int = Query(60, ge=1, le=365),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """List procedures với filter các vấn đề chất lượng dữ liệu."""

    # Subquery đếm chunks
    chunk_count_sq = (
        select(
            DocumentChunk.procedure_code,
            func.count(DocumentChunk.id).label("cnt"),
        )
        .where(DocumentChunk.procedure_code.is_not(None))
        .group_by(DocumentChunk.procedure_code)
        .subquery()
    )

    stmt = (
        select(
            Procedure,
            func.coalesce(chunk_count_sq.c.cnt, 0).label("chunk_count"),
        )
        .outerjoin(chunk_count_sq, chunk_count_sq.c.procedure_code == Procedure.code)
    )

    # Search
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Procedure.name.like(like), Procedure.code.like(like)))

    # Apply issue filter
    if issue == "orphan":
        stmt = stmt.where(func.coalesce(chunk_count_sq.c.cnt, 0) == 0)
    elif issue == "stale":
        threshold = datetime.now(timezone.utc) - timedelta(days=stale_days)
        stmt = stmt.where(Procedure.updated_at < threshold)
    elif issue == "no_steps":
        has_steps = (
            select(ProcedureStep.procedure_id)
            .where(ProcedureStep.procedure_id == Procedure.id)
            .exists()
        )
        stmt = stmt.where(~has_steps)
    elif issue == "failed_forms":
        has_failed = (
            select(ProcedureRequirement.id)
            .where(
                ProcedureRequirement.procedure_id == Procedure.id,
                ProcedureRequirement.form_parse_status.in_(["failed", "unsupported"]),
            )
            .exists()
        )
        stmt = stmt.where(has_failed)
    elif issue == "inactive":
        stmt = stmt.where(Procedure.status == ProcedureStatus.INACTIVE)

    # Count total
    count_stmt = stmt.with_only_columns(func.count()).order_by(None)
    total = (await db.execute(count_stmt)).scalar() or 0

    # Pagination
    stmt = (
        stmt.order_by(Procedure.updated_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )

    rows = (await db.execute(stmt)).all()

    # Per-procedure failed forms detection (subset query để load nhanh)
    proc_ids = [r[0].id for r in rows]
    failed_form_ids: set[str] = set()
    if proc_ids:
        failed_q = await db.execute(
            select(ProcedureRequirement.procedure_id)
            .where(
                ProcedureRequirement.procedure_id.in_(proc_ids),
                ProcedureRequirement.form_parse_status.in_(["failed", "unsupported"]),
            )
            .distinct()
        )
        failed_form_ids = {r[0] for r in failed_q.all()}

    items = [
        AdminProcedureItem(
            id=proc.id,
            code=proc.code,
            name=proc.name,
            domain=proc.domain,
            authority=proc.authority,
            status=proc.status if isinstance(proc.status, str) else proc.status.value,
            chunk_count=int(chunk_cnt),
            has_failed_forms=proc.id in failed_form_ids,
            last_crawled_at=proc.updated_at,
            updated_at=proc.updated_at,
        )
        for proc, chunk_cnt in rows
    ]

    return AdminProcedureListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


# ── Soft delete ─────────────────────────────────────────────────────────────

@router.patch("/{code}/deactivate", response_model=MessageResponse)
async def deactivate_procedure(
    code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Soft delete — set status = INACTIVE. Procedure vẫn còn trong DB nhưng
    không xuất hiện trong tìm kiếm công khai và không retrieve được."""
    result = await db.execute(select(Procedure).where(Procedure.code == code))
    proc = result.scalar_one_or_none()
    if not proc:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy thủ tục {code}")
    proc.status = ProcedureStatus.INACTIVE
    await db.flush()
    return MessageResponse(message=f"Đã vô hiệu hóa thủ tục {code}.")


@router.patch("/{code}/activate", response_model=MessageResponse)
async def activate_procedure(
    code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Khôi phục procedure đã soft delete (status = ACTIVE)."""
    result = await db.execute(select(Procedure).where(Procedure.code == code))
    proc = result.scalar_one_or_none()
    if not proc:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy thủ tục {code}")
    proc.status = ProcedureStatus.ACTIVE
    await db.flush()
    return MessageResponse(message=f"Đã kích hoạt lại thủ tục {code}.")


# ── Bulk re-embed ───────────────────────────────────────────────────────────

@router.post("/bulk/re-embed", response_model=BulkActionResponse)
async def bulk_re_embed(
    payload: BulkActionRequest,
    _: User = Depends(require_admin),
):
    """Enqueue Celery task crawl_single_procedure cho từng code → re-crawl
    từ DVCQG + re-embed. Worker xử lý tuần tự."""
    from app.worker.tasks import crawl_single_procedure

    affected = 0
    failed: list[str] = []
    for code in payload.codes:
        try:
            crawl_single_procedure.delay(code.strip())
            affected += 1
        except Exception:
            failed.append(code)

    return BulkActionResponse(affected=affected, failed_codes=failed)


@router.post("/bulk/deactivate", response_model=BulkActionResponse)
async def bulk_deactivate(
    payload: BulkActionRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Bulk soft delete nhiều procedures."""
    result = await db.execute(
        select(Procedure).where(Procedure.code.in_(payload.codes))
    )
    procs = result.scalars().all()
    found_codes = {p.code for p in procs}
    failed = [c for c in payload.codes if c not in found_codes]

    for p in procs:
        p.status = ProcedureStatus.INACTIVE
    await db.flush()

    return BulkActionResponse(affected=len(procs), failed_codes=failed)
