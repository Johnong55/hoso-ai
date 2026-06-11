# app/schemas/admin.py
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.document import CrawlStatus
from app.models.user import UserRole


class DocumentSourceCreate(BaseModel):
    title: str = Field(..., min_length=2, max_length=300)
    source_url: str = Field(..., min_length=10, max_length=2000)
    source_type: str = Field(..., pattern="^(dichvucong|local|manual)$")
    crawl_frequency: str = Field(default="weekly", pattern="^(daily|weekly|monthly|manual)$")


class DocumentSourceResponse(BaseModel):
    id: str
    title: str
    source_url: str
    source_type: str
    is_active: bool
    content_hash: str | None
    last_crawled_at: datetime | None
    next_crawl_at: datetime | None
    crawl_frequency: str
    crawl_status: CrawlStatus
    processing_status: str
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CrawlTriggerRequest(BaseModel):
    source_id: str


class CrawlTriggerResponse(BaseModel):
    task_id: str
    source_id: str
    message: str


# ── Crawl theo bộ/ngành (agency) hoặc theo mã thủ tục ──────────────────────────

class AgencyItem(BaseModel):
    """1 cơ quan (bộ/ngành) lấy từ API DVCQG."""
    id: str
    name: str
    # `code` là `departmentCode` dùng server-side filter khi crawl (vd "G19", "D01").
    # Luôn có giá trị từ endpoint /department/list-with-location.
    code: str
    level: str | None = None


class CrawlAgencyRequest(BaseModel):
    # `agency_code` là `departmentCode` (vd "G19") — backend dùng để filter
    # server-side. Là field bắt buộc cho flow mới.
    agency_code: str = Field(..., min_length=1, max_length=20)
    agency_name: str | None = Field(None, max_length=300)


class CrawlProcedureRequest(BaseModel):
    # Mã TTHC dạng "1.015028", "2.000123"
    code: str = Field(..., pattern=r"^\d+\.\d{4,}$")


class CrawlProvinceRequest(BaseModel):
    """Phase 12: crawl thủ tục cấp tỉnh. province_code là mã DVCQG (H49, H50, ...)."""
    province_code: str = Field(..., min_length=1, max_length=20)
    province_name: str | None = Field(None, max_length=300)


class CrawlByCodeResponse(BaseModel):
    task_id: str
    code: str
    message: str


class SourceProcedureItem(BaseModel):
    """1 thủ tục trong drill-down của 1 source."""
    code: str
    name: str
    domain: str | None = None
    chunk_count: int = 0
    updated_at: datetime | None = None


class SourceProceduresResponse(BaseModel):
    items: list[SourceProcedureItem]
    total: int
    page: int
    page_size: int


class DailyActivityItem(BaseModel):
    """Số liệu 1 ngày — cho line chart hoạt động 7 ngày qua."""
    date: str        # ISO yyyy-mm-dd
    sessions: int    # số phiên mới
    queries: int     # số câu hỏi


class DomainCountItem(BaseModel):
    """Phân bố thủ tục theo lĩnh vực — cho bar chart."""
    domain: str
    count: int


class TopProcedureItem(BaseModel):
    """Top thủ tục được hỏi nhiều / bị rate thấp."""
    code: str
    name: str
    count: int       # số lần được hỏi
    avg_rating: float | None = None  # rating trung bình nếu có


class RAGStatsResponse(BaseModel):
    # Tổng quan
    total_procedures: int
    total_chunks: int
    total_sessions: int
    total_queries: int
    total_users: int = 0
    total_forms_ok: int = 0      # số form đã parse OK (Phase 11)
    total_feedback: int = 0      # tổng số đánh giá
    # Chất lượng
    avg_latency_ms: float
    fallback_rate: float
    avg_score: float
    avg_rating: float = 0.0      # rating trung bình từ feedback (1-5)
    # Visualizations
    daily_activity: list[DailyActivityItem] = []
    domain_distribution: list[DomainCountItem] = []
    top_procedures: list[TopProcedureItem] = []      # hỏi nhiều nhất
    top_low_rated: list[TopProcedureItem] = []       # rating thấp nhất


class AISettingsResponse(BaseModel):
    llm_model: str
    embedding_model: str
    temperature: float
    max_tokens: int
    top_k: int
    score_threshold: float


class AISettingsUpdateRequest(BaseModel):
    temperature: float | None = Field(None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(None, ge=100, le=4000)
    top_k: int | None = Field(None, ge=1, le=20)
    score_threshold: float | None = Field(None, ge=0.0, le=1.0)


class UserAdminResponse(BaseModel):
    id: str
    email: str | None
    full_name: str | None
    role: UserRole
    is_active: bool | None
    email_verified: bool | None
    created_at: datetime | None

    model_config = {"from_attributes": True}


class UserAdminUpdateRequest(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None
