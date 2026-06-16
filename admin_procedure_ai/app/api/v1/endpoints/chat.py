# app/api/v1/endpoints/chat.py
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, get_current_user_optional, get_db
from app.models.user import User
from app.schemas.chat import (
    AskRequest,
    AskResponse,
    CreateSessionRequest,
    FormGuideRequest,
    FormGuideResponse,
    SectionRequest,
    SectionResponse,
    SessionHistoryResponse,
    SessionResponse,
)
from app.schemas.common import MessageResponse, PaginatedResponse
from app.services.chat.chat_service import ChatService
from app.services.chat.guest_rate_limit import (
    GUEST_DAILY_LIMIT,
    check_and_increment,
)

router = APIRouter(prefix="/chat", tags=["Chat"])


def _get_client_ip(request: Request) -> str:
    """Lấy IP thực của client, ưu tiên X-Forwarded-For (qua Cloudflare Tunnel)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/ask", response_model=AskResponse)
async def ask(
    payload: AskRequest,
    request: Request,
    current_user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Ask a natural language question about an administrative procedure.
    Works for both guests (no session saved to DB) and authenticated users.

    Guests bị giới hạn 10 câu/ngày theo IP để tránh lạm dụng tài nguyên LLM.
    User đăng nhập không bị giới hạn ở tầng này.
    """
    if current_user is None:
        ip = _get_client_ip(request)
        allowed, current, limit = await check_and_increment(ip)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "message": (
                        f"Bạn đã đạt giới hạn {limit} câu hỏi miễn phí trong ngày. "
                        f"Vui lòng đăng ký tài khoản để tiếp tục sử dụng không giới hạn."
                    ),
                    "current": current,
                    "limit": limit,
                    "reason": "guest_daily_limit_exceeded",
                },
            )

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

    Phase 9: check Redis cache trước → instant nếu pre-cache đã xong từ task
    background fired sau /chat/ask. Miss → fall back live LLM.
    """
    service = ChatService(db)
    return await service.request_section(payload, current_user)


@router.post("/form-guide", response_model=FormGuideResponse)
async def request_form_guide(
    payload: FormGuideRequest,
    current_user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Phase 11: User click nút "📝 Hướng dẫn điền" trên 1 form card → LLM sinh
    hướng dẫn 2 phần (Tóm tắt + Chi tiết từng mục) từ nội dung file biểu mẫu
    đã parse sẵn lúc crawl (form_content_text + form_fields_json).

    Idempotent — click cùng form 2 lần trả lại nội dung cũ. Trả message giải
    thích nếu form chưa parse / parse fail / định dạng không hỗ trợ.
    """
    service = ChatService(db)
    return await service.request_form_guide(payload, current_user)


@router.get("/section/status")
async def section_cache_status(
    session_id: str,
    procedure_code: str,
    sections: str,
    _: User | None = Depends(get_current_user_optional),
):
    """
    Phase 9: check sections nào đã có cache. FE poll mỗi 1-2s sau /chat/ask
    để show icon "ready" trên chip → user biết click sẽ instant.

    `sections`: comma-separated list, vd "steps,requirements,fees,agency,forms".
    """
    from app.services.chat.section_cache import get_status
    section_list = [s.strip() for s in sections.split(",") if s.strip()]
    cache_map = await get_status(session_id, procedure_code, section_list)
    return {"procedure_code": procedure_code, "ready": cache_map}


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
