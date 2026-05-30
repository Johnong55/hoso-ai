# app/schemas/chat.py
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.conversation import MessageRole


class ConversationTurn(BaseModel):
    """1 lượt hội thoại — dùng cho guest gửi history từ localStorage để giữ multi-turn."""
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., max_length=4000)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)
    session_id: str | None = None
    locality: str | None = Field(None, max_length=200)
    domain: str | None = Field(None, max_length=200)
    # Chỉ guest dùng: gửi lịch sử inline từ localStorage để rewrite_query hiểu
    # ngữ cảnh follow-up. User đã đăng nhập thì BE tự load từ DB.
    history: list[ConversationTurn] = []


class SourceItem(BaseModel):
    chunk_id: str
    procedure_id: str | None
    procedure_code: str | None
    procedure_name: str | None
    chunk_type: str
    content_preview: str
    score: float


class FormItem(BaseModel):
    """Biểu mẫu/tờ khai có thể tải về, liên quan tới thủ tục trong câu trả lời."""
    name: str                       # tên giấy tờ (vd: Tờ khai NC14)
    form_name: str | None = None    # tên file (vd: Phlcs01.docx)
    url: str                        # link tải trực tiếp
    procedure_code: str | None = None
    procedure_name: str | None = None


class AskResponse(BaseModel):
    answer: str
    session_id: str
    message_id: str
    sources: list[SourceItem]
    forms: list[FormItem] = []
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
    # Forms re-derived từ audit (RAGGenerationLog → RAGRetrieval → ProcedureRequirement)
    # khi load session history → giữ nút "Tải về" sau khi navigate đi/về.
    forms: list["FormItem"] = []

    model_config = {"from_attributes": True}


class SessionHistoryResponse(BaseModel):
    session: SessionResponse
    messages: list[MessageResponse]


class CreateSessionRequest(BaseModel):
    locality: str | None = Field(None, max_length=200)
    domain: str | None = Field(None, max_length=200)
