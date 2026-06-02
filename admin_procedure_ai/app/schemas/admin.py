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
    code: str | None = None


class CrawlAgencyRequest(BaseModel):
    agency_id: str = Field(..., min_length=1, max_length=50)
    agency_name: str | None = Field(None, max_length=300)


class CrawlProcedureRequest(BaseModel):
    # Mã TTHC dạng "1.015028", "2.000123"
    code: str = Field(..., pattern=r"^\d+\.\d{4,}$")


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


class RAGStatsResponse(BaseModel):
    total_procedures: int
    total_chunks: int
    total_sessions: int
    total_queries: int
    avg_latency_ms: float
    fallback_rate: float
    avg_score: float


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
