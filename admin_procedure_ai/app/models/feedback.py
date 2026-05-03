# app/models/feedback.py
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Feedback(Base):
    # DD: table name = feedback_records
    __tablename__ = "feedback_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # DD: message_id — đánh giá câu trả lời nào
    message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("messages.id"), nullable=True, index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True, index=True
    )
    procedure_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("procedures.id"), nullable=True, index=True
    )

    # DD: rating INT (1-5)
    rating: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)

    # DD: is_reviewed — admin đã xem xét chưa
    is_reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    # DD: reviewed_by — admin đã review
    reviewed_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    admin_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(
        back_populates="feedback", foreign_keys=[user_id], lazy="noload"
    )
    procedure: Mapped["Procedure"] = relationship(back_populates="feedback", lazy="noload")
