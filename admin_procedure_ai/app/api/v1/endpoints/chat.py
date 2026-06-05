# app/api/v1/endpoints/chat.py
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, get_current_user_optional, get_db
from app.models.user import User
from app.schemas.chat import (
    AskRequest,
    AskResponse,
    CreateSessionRequest,
    SectionRequest,
    SectionResponse,
    SessionHistoryResponse,
    SessionResponse,
)
from app.schemas.common import MessageResponse, PaginatedResponse
from app.services.chat.chat_service import ChatService

router = APIRouter(prefix="/chat", tags=["Chat"])


@router.post("/ask", response_model=AskResponse)
async def ask(
    payload: AskRequest,
    current_user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Ask a natural language question about an administrative procedure.
    Works for both guests (no session saved to DB) and authenticated users.
    """
    service = ChatService(db)
    return await service.ask(payload, current_user)


@router.post("/section", response_model=SectionResponse)
async def request_section(
    payload: SectionRequest,
    current_user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    User click 1 chip → backend format section đó cho thủ tục đã xác định.
    Result append vào session hiện tại như 1 assistant message mới (nếu user
    đã đăng nhập + có session_id). Guest dùng session local trên FE.
    """
    service = ChatService(db)
    return await service.request_section(payload, current_user)


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: CreateSessionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Tạo phiên hội thoại mới (yêu cầu đăng nhập)."""
    service = ChatService(db)
    return await service.create_session(payload, current_user)


@router.get("/sessions", response_model=PaginatedResponse[SessionResponse])
async def list_sessions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lấy danh sách phiên hội thoại của người dùng."""
    service = ChatService(db)
    return await service.list_user_sessions(current_user, page, page_size)


@router.get("/sessions/{session_id}", response_model=SessionHistoryResponse)
async def get_session_history(
    session_id: str,
    current_user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Lấy lịch sử tin nhắn của một phiên hội thoại."""
    service = ChatService(db)
    return await service.get_session_history(session_id, current_user)


@router.delete("/sessions/{session_id}", response_model=MessageResponse)
async def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Xoá (ẩn) phiên hội thoại."""
    service = ChatService(db)
    await service.delete_session(session_id, current_user)
    return MessageResponse(message="Đã xoá phiên hội thoại.")
