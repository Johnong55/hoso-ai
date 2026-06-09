# app/models/procedure.py
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Integer,
    String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ProcedureStatus(str, enum.Enum):
    DRAFT    = "draft"
    ACTIVE   = "active"
    INACTIVE = "inactive"   # = expired / replaced — theo DD
    EXPIRED  = "expired"    # giữ để tương thích ngược
    REPLACED = "replaced"   # giữ để tương thích ngược


class AuthorityLevel(str, enum.Enum):
    CENTRAL    = "central"
    PROVINCIAL = "provincial"
    DISTRICT   = "district"
    COMMUNE    = "commune"


class Procedure(Base):
    __tablename__ = "procedures"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    domain: Mapped[str | None] = mapped_column(String(100), index=True)
    description: Mapped[str | None] = mapped_column(Text)

    # Cơ quan thực hiện (DD: authority)
    authority: Mapped[str | None] = mapped_column(String(255))
    # Giữ implementing_agency để tương thích với crawler & service hiện tại
    implementing_agency: Mapped[str | None] = mapped_column(String(255))
    coordinating_agency: Mapped[str | None] = mapped_column(String(255))

    authority_level: Mapped[str] = mapped_column(
        Enum(AuthorityLevel, values_callable=lambda x: [e.value for e in x]),
        default=AuthorityLevel.CENTRAL,
        nullable=False,
    )

    # Versioning
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("procedures.id"), nullable=True)
    replaced_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    effective_date: Mapped[datetime | None] = mapped_column(Date)
    expired_date: Mapped[datetime | None] = mapped_column(Date)

    legal_basis: Mapped[str | None] = mapped_column(Text)

    # DD: processing_days (INT), nhưng dùng VARCHAR cho linh hoạt (vd: "07 Ngày làm việc")
    processing_time: Mapped[str | None] = mapped_column(String(200))
    # DD: fee DECIMAL(10,2), nhưng VARCHAR linh hoạt hơn (vd: "20.000 Đồng; Miễn phí")
    fee: Mapped[str | None] = mapped_column(String(500))
    result: Mapped[str | None] = mapped_column(Text)

    # SHA256 hex của parsed content (steps + fees + requirements + ...)
    # Dùng cho change detection: nếu hash giống lần crawl trước → SKIP re-embed,
    # tiết kiệm quota embedding API.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    # `updatedAt` (epoch milliseconds) lấy trực tiếp từ JSON API DVCQG.
    # Ưu tiên dùng field này cho change detection: chỉ cần list-all rồi so sánh
    # với DB, không phải tải full detail mới biết có thay đổi hay không.
    source_updated_at: Mapped[int | None] = mapped_column(BigInteger, index=True)

    # UUID gốc của thủ tục bên DVCQG (field `data.id` từ API detail). Dùng
    # build URL nộp trực tuyến: dichvucong.gov.vn/...?formalityId=<UUID>
    formality_id: Mapped[str | None] = mapped_column(String(40), index=True)

    status: Mapped[str] = mapped_column(
        Enum(ProcedureStatus, values_callable=lambda x: [e.value for e in x]),
        default=ProcedureStatus.DRAFT,
        nullable=False,
        index=True,
    )

    # Audit
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    requirements: Mapped[list["ProcedureRequirement"]] = relationship(
        back_populates="procedure", cascade="all, delete-orphan", lazy="noload"
    )
    steps: Mapped[list["ProcedureStep"]] = relationship(
        back_populates="procedure",
        cascade="all, delete-orphan",
        order_by="ProcedureStep.step_order",
        lazy="noload",
    )
    fees: Mapped[list["ProcedureFee"]] = relationship(
        back_populates="procedure",
        cascade="all, delete-orphan",
        order_by="ProcedureFee.order",
        lazy="noload",
    )
    localities: Mapped[list["ProcedureLocality"]] = relationship(
        back_populates="procedure", cascade="all, delete-orphan", lazy="noload"
    )
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="procedure", cascade="all, delete-orphan", lazy="noload"
    )
    sources: Mapped[list["DocumentSource"]] = relationship(
        back_populates="procedure", lazy="noload"
    )
    feedback: Mapped[list["Feedback"]] = relationship(back_populates="procedure", lazy="noload")


class ProcedureRequirement(Base):
    __tablename__ = "procedure_requirements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    procedure_id: Mapped[str] = mapped_column(String(36), ForeignKey("procedures.id"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # DD: description — mô tả chi tiết (bản gốc/sao, công chứng, số lượng...)
    description: Mapped[str | None] = mapped_column(Text)
    # Tên trường hợp hồ sơ (ví dụ: "Đăng ký tại chỗ ở thuê, mượn, ở nhờ")
    # Dùng để group requirements khi chunking → tránh 28 chunks giống nhau
    case_group: Mapped[str | None] = mapped_column(String(500))
    # Các field bổ sung hữu ích (không có trong DD nhưng crawler cần)
    form_name: Mapped[str | None] = mapped_column(String(300))
    form_url: Mapped[str | None] = mapped_column(Text)      # link tải biểu mẫu
    quantity: Mapped[str | None] = mapped_column(String(100))
    document_type: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    # DD: is_required; code dùng is_mandatory (rõ nghĩa hơn)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Phase 11 — content parse từ file biểu mẫu (DOCX/PDF/XLS), dùng để LLM
    # sinh hướng dẫn điền form. NULL status = chưa parse → backfill index.
    form_content_text: Mapped[str | None] = mapped_column(Text)
    form_fields_json: Mapped[list | None] = mapped_column(JSON)
    form_parsed_at: Mapped[datetime | None] = mapped_column(DateTime)
    form_parse_status: Mapped[str | None] = mapped_column(String(20), index=True)

    procedure: Mapped["Procedure"] = relationship(back_populates="requirements", lazy="noload")


class ProcedureStep(Base):
    __tablename__ = "procedure_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    procedure_id: Mapped[str] = mapped_column(String(36), ForeignKey("procedures.id"), nullable=False, index=True)
    # DD: step_order — tránh xung đột với keyword SQL "ORDER"
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    responsible_party: Mapped[str | None] = mapped_column(String(300))
    duration: Mapped[str | None] = mapped_column(String(100))

    procedure: Mapped["Procedure"] = relationship(back_populates="steps", lazy="noload")


class ProcedureFee(Base):
    """
    Phí/lệ phí của thủ tục theo từng phương thức nộp hồ sơ.
    Một procedure thường có nhiều tier:
      - Trực tiếp / Trực tuyến / Dịch vụ bưu chính
      - Mỗi phương thức lại có nhiều mức (vd: 0đ, 5tr, 10tr tuỳ trường hợp)
    """
    __tablename__ = "procedure_fees"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    procedure_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("procedures.id"), nullable=False, index=True
    )

    # "Trực tiếp" | "Trực tuyến" | "Dịch vụ bưu chính" | ...
    submission_method: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # "30 Ngày" | "07 Ngày làm việc" | ...
    processing_time: Mapped[str | None] = mapped_column(String(200))
    # "5 triệu Đồng" | "0 Đồng" | "Miễn phí"
    amount_text: Mapped[str | None] = mapped_column(String(300))
    # Mô tả trường hợp áp dụng mức phí này
    description: Mapped[str | None] = mapped_column(Text)
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    procedure: Mapped["Procedure"] = relationship(back_populates="fees", lazy="noload")


class Locality(Base):
    __tablename__ = "localities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False)  # province | district | commune
    parent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("localities.id"))

    procedure_localities: Mapped[list["ProcedureLocality"]] = relationship(
        back_populates="locality", lazy="noload"
    )


class ProcedureLocality(Base):
    __tablename__ = "procedure_localities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    procedure_id: Mapped[str] = mapped_column(String(36), ForeignKey("procedures.id"), nullable=False)
    locality_id: Mapped[str] = mapped_column(String(36), ForeignKey("localities.id"), nullable=False)
    # DD: override_days (INT) — dùng VARCHAR cho linh hoạt
    override_days: Mapped[str | None] = mapped_column(String(200))
    override_fee: Mapped[str | None] = mapped_column(String(500))
    note: Mapped[str | None] = mapped_column(Text)

    procedure: Mapped["Procedure"] = relationship(back_populates="localities", lazy="noload")
    locality: Mapped["Locality"] = relationship(back_populates="procedure_localities", lazy="noload")
