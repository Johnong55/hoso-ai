# app/schemas/chat.py
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.conversation import MessageRole


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)
    session_id: str | None = None
    locality: str | None = Field(None, max_length=200)
    domain: str | None = Field(None, max_length=200)


class SourceItem(BaseModel):
    chunk_id: str
    procedure_id: str | None
    procedure_code: str | None
    procedure_name: str | None
    chunk_type: str
    content_preview: str
    score: float


class AskResponse(BaseModel):
    answer: str
    session_id: str
    message_id: str
    sources: list[SourceItem]
    is_fallback: bool
    latency_ms: int


class SessionResponse(BaseModel):
    id: str
    title: str | None
    is_guest: bool
    locality_filter: str | None
    domain_filter: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    message_count: int = 0

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: MessageRole
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SessionHistoryResponse(BaseModel):
    session: SessionResponse
    messages: list[MessageResponse]


class CreateSessionRequest(BaseModel):
    locality: str | None = Field(None, max_length=200)
    domain: str | None = Field(None, max_length=200)
