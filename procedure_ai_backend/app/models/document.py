# app/models/document.py
import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CrawlFrequency(str, enum.Enum):
    """Tần suất tự động crawl lại nguồn (DD: crawl_frequency)."""
    DAILY   = "daily"
    WEEKLY  = "weekly"
    MONTHLY = "monthly"
    MANUAL  = "manual"


class ProcessingStatus(str, enum.Enum):
    """
    Trạng thái pipeline RAG của document source (DD: processing_status).
    pending → chunked → embedded. failed = cần retry.
    """
    PENDING  = "pending"
    CHUNKED  = "chunked"
    EMBEDDED = "embedded"
    FAILED   = "failed"


class CrawlStatus(str, enum.Enum):
    """Trạng thái crawl web (riêng biệt với processing_status)."""
    PENDING  = "pending"
    CRAWLING = "crawling"
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"


class EmbeddingStatus(str, enum.Enum):
    """Trạng thái tạo vector embedding của từng chunk (DD: embedding_status)."""
    PENDING = "pending"
    DONE    = "done"
    ERROR   = "error"


class ChunkType(str, enum.Enum):
    GENERAL     = "general"
    REQUIREMENT = "requirement"
    STEP        = "step"
    FEE         = "fee"
    RESULT      = "result"
    LEGAL_BASIS = "legal_basis"


class DocumentSource(Base):
    __tablename__ = "document_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # DD: procedure_id — nguồn này thuộc thủ tục nào
    procedure_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("procedures.id"), nullable=True, index=True
    )

    # DD: title (was: name)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    # DD: source_url (was: url)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)

    source_type: Mapped[str] = mapped_column(String(50), nullable=False)  # dichvucong | local | manual
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Change detection
    content_hash: Mapped[str | None] = mapped_column(String(64))      # SHA256 hex
    change_detected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Versioning của source
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    # Crawl scheduling
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_crawl_at: Mapped[datetime | None] = mapped_column(DateTime)
    crawl_frequency: Mapped[CrawlFrequency] = mapped_column(
        Enum(CrawlFrequency, values_callable=lambda x: [e.value for e in x]),
        default=CrawlFrequency.WEEKLY,
        nullable=False,
    )

    # Status tracking
    crawl_status: Mapped[CrawlStatus] = mapped_column(
        Enum(CrawlStatus, values_callable=lambda x: [e.value for e in x]),
        default=CrawlStatus.PENDING,
        nullable=False,
    )
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus, values_callable=lambda x: [e.value for e in x]),
        default=ProcessingStatus.PENDING,
        nullable=False,
        index=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    procedure: Mapped["Procedure"] = relationship(back_populates="sources", lazy="noload")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", lazy="noload"
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id: Mapped[str] = mapped_column(String(36), ForeignKey("document_sources.id"), nullable=False, index=True)
    # Denormalized FK để tránh JOIN khi filter
    procedure_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("procedures.id"), nullable=True, index=True
    )

    # ChromaDB reference — embedding thực nằm trong Chroma
    vector_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_type: Mapped[ChunkType] = mapped_column(
        Enum(ChunkType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        index=True,
    )

    # Denormalized metadata cho pre-filter trong Chroma
    procedure_code: Mapped[str | None] = mapped_column(String(100), index=True)
    domain: Mapped[str | None] = mapped_column(String(200), index=True)
    authority_level: Mapped[str | None] = mapped_column(String(50), index=True)
    locality: Mapped[str | None] = mapped_column(String(200), index=True)
    section: Mapped[str | None] = mapped_column(String(255))
    step_order: Mapped[int | None] = mapped_column(Integer)

    # Pipeline tracking
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    token_count: Mapped[int | None] = mapped_column(Integer)
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    # DD: embedding_status — trạng thái tạo vector
    embedding_status: Mapped[EmbeddingStatus] = mapped_column(
        Enum(EmbeddingStatus, values_callable=lambda x: [e.value for e in x]),
        default=EmbeddingStatus.PENDING,
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    source: Mapped["DocumentSource"] = relationship(back_populates="chunks", lazy="noload")
    procedure: Mapped["Procedure"] = relationship(back_populates="chunks", lazy="noload")
    retrievals: Mapped[list["RAGRetrieval"]] = relationship(back_populates="chunk", lazy="noload")
