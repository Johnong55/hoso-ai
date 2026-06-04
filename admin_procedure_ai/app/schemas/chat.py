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


# Kiểu section người dùng có thể tra cứu cho 1 thủ tục.
# Frontend render chip dựa trên list này.
SECTION_TYPES = {
    "steps": "Trình tự thực hiện",
    "requirements": "Giấy tờ cần chuẩn bị",
    "fees": "Lệ phí & thời hạn",
    "agency": "Cơ quan thực hiện",
    "forms": "Biểu mẫu tải về",
    "other_procedures": "Xem thủ tục khác",
}


class RelatedProcedure(BaseModel):
    """Thủ tục liên quan (TOP-2, TOP-3) — chip 'Xem thủ tục khác' dẫn tới."""
    code: str
    name: str


class ProcedureFocus(BaseModel):
    """
    Khi AI xác định được 1 thủ tục phù hợp tình huống, trả về structured
    info để FE render chip row → user click → fetch section content.
    """
    code: str                                      # mã thủ tục (vd "1.003460")
    name: str                                      # tên đầy đủ
    available_chips: list[str]                     # subset của SECTION_TYPES keys
    related: list[RelatedProcedure] = []           # thủ tục liên quan để chip "Xem thủ tục khác"
    # URL Cổng DVCQG để user click → nộp trực tuyến. Null nếu thủ tục không
    # hỗ trợ submission ONLINE hoặc chưa có formality_id (data cũ).
    online_submission_url: str | None = None


class AskResponse(BaseModel):
    answer: str
    session_id: str
    message_id: str
    sources: list[SourceItem]
    forms: list[FormItem] = []
    is_fallback: bool
    latency_ms: int
    # null khi AI không xác định được 1 thủ tục cụ thể (vd câu hỏi mơ hồ,
    # query general). FE chỉ render chip khi field này có giá trị.
    procedure_focus: ProcedureFocus | None = None


class SectionRequest(BaseModel):
    """
    User click chip → request content cho 1 section của thủ tục đã chọn.
    Append vào session hiện tại như 1 assistant message mới.
    """
    session_id: str | None = None
    procedure_code: str = Field(..., min_length=1, max_length=20)
    section_type: str = Field(..., min_length=1, max_length=30)


class SectionResponse(BaseModel):
    """Response cho 1 chip click. Content được persist vào DB như assistant msg."""
    answer: str
    session_id: str
    message_id: str
    forms: list[FormItem] = []
    procedure_code: str
    section_type: str
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
    # Cùng lý do với forms: re-derive chip để giữ tương tác sau navigate
    # (TOP-1 procedure tại lúc generate). Null nếu message không phải intro.
    procedure_focus: ProcedureFocus | None = None

    model_config = {"from_attributes": True}


class SessionHistoryResponse(BaseModel):
    session: SessionResponse
    messages: list[MessageResponse]


class CreateSessionRequest(BaseModel):
    locality: str | None = Field(None, max_length=200)
    domain: str | None = Field(None, max_length=200)
