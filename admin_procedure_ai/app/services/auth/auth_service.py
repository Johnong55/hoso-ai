# app/services/auth/auth_service.py
from datetime import datetime, timezone

from fastapi import HTTPException, status
from jose import JWTError
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserResponse,
)


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def register(self, payload: RegisterRequest) -> UserResponse:
        existing = await self._db.execute(select(User).where(User.email == payload.email))
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email đã được sử dụng.",
            )

        user = User(
            email=payload.email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name,
        )
        self._db.add(user)
        await self._db.flush()
        await self._db.refresh(user)   # reload server_default values (created_at, updated_at)
        logger.info(f"Auth | register | user_id={user.id} email={user.email}")
        return UserResponse.model_validate(user)

    async def login(self, payload: LoginRequest) -> TokenResponse:
        result = await self._db.execute(select(User).where(User.email == payload.email))
        user = result.scalar_one_or_none()

        if user is None or not verify_password(payload.password, user.password_hash or ""):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Email hoặc mật khẩu không đúng.",
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tài khoản của bạn đã bị vô hiệu hóa.",
            )

        if user.locked_until and user.locked_until > datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tài khoản tạm thời bị khóa. Vui lòng thử lại sau.",
            )

        # Reset login fail count on success
        user.login_fail_count = 0
        user.last_login_at = datetime.now(timezone.utc)

        access_token = create_access_token(user.id, user.role.value)
        refresh_token = create_refresh_token(user.id)
        logger.info(f"Auth | login | user_id={user.id}")
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    async def refresh(self, payload: RefreshRequest) -> TokenResponse:
        try:
            token_data = decode_token(payload.refresh_token)
            if token_data.get("type") != "refresh":
                raise ValueError("Not a refresh token")
            user_id: str = token_data["sub"]
        except (JWTError, KeyError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Refresh token không hợp lệ hoặc đã hết hạn.",
            )

        result = await self._db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Người dùng không tồn tại hoặc đã bị vô hiệu hóa.",
            )

        access_token = create_access_token(user.id, user.role.value)
        new_refresh_token = create_refresh_token(user.id)
        return TokenResponse(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    async def change_password(self, user: User, payload: ChangePasswordRequest) -> None:
        if not verify_password(payload.current_password, user.password_hash or ""):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Mật khẩu hiện tại không đúng.",
            )
        user.password_hash = hash_password(payload.new_password)
        logger.info(f"Auth | change_password | user_id={user.id}")

    async def update_profile(self, user: User, payload: UpdateProfileRequest) -> UserResponse:
        if payload.full_name is not None:
            user.full_name = payload.full_name
        if payload.phone is not None:
            user.phone = payload.phone
        await self._db.flush()
        await self._db.refresh(user)
        return UserResponse.model_validate(user)
