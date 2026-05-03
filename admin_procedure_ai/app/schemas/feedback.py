# app/schemas/feedback.py
from datetime import datetime

from pydantic import BaseModel, Field


class FeedbackCreateRequest(BaseModel):
    procedure_id: str | None = None
    message_id: str | None = None
    rating: int | None = Field(None, ge=1, le=5)
    comment: str | None = Field(None, max_length=2000)


class FeedbackResponse(BaseModel):
    id: str
    user_id: str | None
    procedure_id: str | None
    message_id: str | None
    rating: int | None
    comment: str | None
    is_reviewed: bool
    reviewed_at: datetime | None
    admin_note: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class FeedbackAdminUpdateRequest(BaseModel):
    """Admin đánh dấu đã xem xét và ghi chú."""
    admin_note: str | None = Field(None, max_length=2000)
