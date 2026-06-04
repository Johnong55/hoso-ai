"""add message section_type to verify viewed chips

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-04

Thêm cột `messages.section_type` để đánh dấu message sinh từ click chip
(steps / requirements / fees / agency / forms / other_procedures).
Cùng giá trị set trên USER message (label chip user "ấn") + ASSISTANT
message (nội dung section LLM format). Giúp:
  1. Sau reload, FE biết chip nào đã click → ẩn khỏi dock, không click lại
  2. Click cùng chip 2 lần không tạo 2 output khác nhau (idempotent UX)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("section_type", sa.String(30), nullable=True),
    )
    op.create_index(
        "ix_messages_section_type",
        "messages",
        ["section_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_section_type", table_name="messages")
    op.drop_column("messages", "section_type")
