# app/models/conversation.py
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MessageRole(str, enum.Enum):
    USER      = "user"
    ASSISTANT = "assistant"
    SYSTEM    = "system"


class ConversationSession(Base):
    __tablename__ = "conversation_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    # DD: message_count — tổng số tin nhắn trong session
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locality_filter: Mapped[str | None] = mapped_column(String(200))
    domain_filter: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="sessions", lazy="noload")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        lazy="noload",
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversation_sessions.id"), nullable=False, index=True
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # DD: token_count — số tokens của content
    token_count: Mapped[int | None] = mapped_column(Integer)
    # Loại section nếu message này sinh từ click chip (vd "steps", "requirements").
    # Null cho message thường (user input + assistant intro). Cùng giá trị trên
    # cả USER msg (label chip user click) lẫn ASSISTANT msg (nội dung section)
    # để dễ pair lại lúc render.
    section_type: Mapped[str | None] = mapped_column(String(60), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    session: Mapped["ConversationSession"] = relationship(back_populates="messages", lazy="noload")
    rag_query: Mapped["RAGQuery | None"] = relationship(back_populates="message", lazy="noload")


class RAGQuery(Base):
    __tablename__ = "rag_queries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id"), nullable=False, unique=True, index=True
    )
    original_query: Mapped[str] = mapped_column(Text, nullable=False)
    rewritten_query: Mapped[str | None] = mapped_column(Text)
    # DD: vector_id — ID của query embedding trong Chroma
    vector_id: Mapped[str | None] = mapped_column(String(255))
    locality_filter: Mapped[str | None] = mapped_column(String(200))
    domain_filter: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    message: Mapped["Message"] = relationship(back_populates="rag_query", lazy="noload")
    retrievals: Mapped[list["RAGRetrieval"]] = relationship(
        back_populates="rag_query", cascade="all, delete-orphan", lazy="noload"
    )
    generation_log: Mapped["RAGGenerationLog | None"] = relationship(
        back_populates="rag_query", lazy="noload"
    )


class RAGRetrieval(Base):
    __tablename__ = "rag_retrievals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # DD: query_id (was: rag_query_id)
    query_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("rag_queries.id"), nullable=False, index=True
    )
    chunk_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_chunks.id"), nullable=False, index=True
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    rerank_score: Mapped[float | None] = mapped_column(Float)
    # DD: retrieval_method — vector | bm25 | hybrid
    retrieval_method: Mapped[str] = mapped_column(String(50), default="vector", nullable=False)
    # DD: rank_order (was: rank)
    rank_order: Mapped[int] = mapped_column(Integer, nullable=False)
    was_used: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    rag_query: Mapped["RAGQuery"] = relationship(back_populates="retrievals", lazy="noload")
    chunk: Mapped["DocumentChunk"] = relationship(back_populates="retrievals", lazy="noload")


class RAGGenerationLog(Base):
    __tablename__ = "rag_generation_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    rag_query_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("rag_queries.id"), nullable=False, unique=True, index=True
    )
    # DD: message_id — link trực tiếp đến message kết quả
    message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("messages.id"), nullable=True, index=True
    )

    model: Mapped[str] = mapped_column(String(100), nullable=False)
    system_prompt: Mapped[str | None] = mapped_column(Text)   # giữ để debug
    # DD: prompt (was: full_prompt)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)

    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    # DD: response_time FLOAT (giây) — (was: latency_ms INT)
    response_time: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    rag_query: Mapped["RAGQuery"] = relationship(back_populates="generation_log", lazy="noload")
