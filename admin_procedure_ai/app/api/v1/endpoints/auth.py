# app/api/v1/endpoints/auth.py
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, get_db
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserResponse,
)
from app.schemas.common import MessageResponse
from app.services.auth.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Đăng ký tài khoản mới."""
    service = AuthService(db)
    return await service.register(payload)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Đăng nhập và nhận JWT tokens."""
    service = AuthService(db)
    return await service.login(payload)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """Làm mới access token bằng refresh token."""
    service = AuthService(db)
    return await service.refresh(payload)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """Lấy thông tin tài khoản hiện tại."""
    return UserResponse.model_validate(current_user)


@router.put("/me", response_model=UserResponse)
async def update_profile(
    payload: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cập nhật thông tin cá nhân."""
    service = AuthService(db)
    return await service.update_profile(current_user, payload)


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Đổi mật khẩu."""
    service = AuthService(db)
    await service.change_password(current_user, payload)
    return MessageResponse(message="Đổi mật khẩu thành công.")


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    payload: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Yêu cầu gửi email đặt lại mật khẩu.

    Luôn trả về thông báo thành công để tránh lộ thông tin email nào đã đăng ký
    (phòng chống email enumeration). Email chỉ thực sự được gửi nếu tài khoản
    tồn tại và đang hoạt động.
    """
    service = AuthService(db)
    await service.request_password_reset(payload)
    return MessageResponse(
        message="Nếu email tồn tại trong hệ thống, một liên kết đặt lại mật khẩu đã được gửi đến hộp thư của bạn."
    )


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Đặt lại mật khẩu bằng token nhận được qua email."""
    service = AuthService(db)
    await service.reset_password(payload)
    return MessageResponse(message="Đặt lại mật khẩu thành công. Bạn có thể đăng nhập ngay.")
