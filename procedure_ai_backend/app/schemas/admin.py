# app/schemas/admin.py
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.document import CrawlStatus
from app.models.user import UserRole


class DocumentSourceCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=300)
    url: str = Field(..., min_length=10, max_length=2000)
    source_type: str = Field(..., pattern="^(dichvucong|local|manual)$")
    crawl_interval_hours: int = Field(default=24, ge=1, le=720)


class DocumentSourceResponse(BaseModel):
    id: str
    name: str
    url: str
    source_type: str
    is_active: bool
    content_hash: str | None
    last_crawled_at: datetime | None
    crawl_status: CrawlStatus
    error_message: str | None
    crawl_interval_hours: int
    created_at: datetime

    model_config = {"from_attributes": True}


class CrawlTriggerRequest(BaseModel):
    source_id: str


class CrawlTriggerResponse(BaseModel):
    task_id: str
    source_id: str
    message: str


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
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    is_verified: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserAdminUpdateRequest(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None
