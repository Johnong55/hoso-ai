# app/api/v1/endpoints/admin/users.py
import math

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, require_admin
from app.models.user import User
from app.schemas.admin import UserAdminResponse, UserAdminUpdateRequest
from app.schemas.common import PaginatedResponse

router = APIRouter(prefix="/users", tags=["Admin - Users"])


@router.get("", response_model=PaginatedResponse[UserAdminResponse])
async def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    total = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    offset = (page - 1) * page_size
    result = await db.execute(select(User).order_by(User.created_at.desc()).offset(offset).limit(page_size))
    users = result.scalars().all()
    return PaginatedResponse(
        items=[UserAdminResponse.model_validate(u) for u in users],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.patch("/{user_id}", response_model=UserAdminResponse)
async def update_user(
    user_id: str,
    payload: UserAdminUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Người dùng không tồn tại.")
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    return UserAdminResponse.model_validate(user)
