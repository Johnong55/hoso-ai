# app/api/v1/endpoints/feedback.py
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, get_current_user_optional, get_db
from app.models.feedback import Feedback
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.feedback import FeedbackCreateRequest, FeedbackResponse

router = APIRouter(prefix="/feedback", tags=["Feedback"])


@router.post("", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    payload: FeedbackCreateRequest,
    current_user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Gửi phản hồi về câu trả lời hoặc thủ tục. Cho phép cả khách và người dùng đã đăng nhập."""
    feedback = Feedback(
        user_id=current_user.id if current_user else None,
        procedure_id=payload.procedure_id,
        message_id=payload.message_id,
        rating=payload.rating,
        comment=payload.comment,
    )
    db.add(feedback)
    await db.flush()
    await db.refresh(feedback)
    return FeedbackResponse.model_validate(feedback)
