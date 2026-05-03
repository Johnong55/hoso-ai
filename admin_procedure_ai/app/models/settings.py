# app/models/settings.py
"""
Bảng ai_settings và system_logs theo Data Dictionary.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AISettings(Base):
    """
    Cấu hình AI/LLM — admin có thể thay đổi không cần deploy lại.
    Chỉ 1 record is_active=TRUE tại một thời điểm.
    """
    __tablename__ = "ai_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    config_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    # LLM config
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="gpt-4o-mini")
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.1)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)

    # RAG config
    rag_top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    rag_score_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.65)

    # System prompt — admin chỉnh trực tiếp, không cần deploy
    system_prompt: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SystemLog(Base):
    """
    Log hệ thống tổng hợp — lưu các sự kiện quan trọng.
    """
    __tablename__ = "system_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    level: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # DEBUG|INFO|WARNING|ERROR|CRITICAL
    service: Mapped[str] = mapped_column(String(100), nullable=False, index=True)  # crawler|rag|auth|embedder
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Context (optional)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    extra: Mapped[str | None] = mapped_column(Text)  # JSON extra context

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False, index=True)
