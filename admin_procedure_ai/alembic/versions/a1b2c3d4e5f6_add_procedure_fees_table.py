"""add_procedure_fees_table

Revision ID: a1b2c3d4e5f6
Revises: f3c1a2b4d8e0
Create Date: 2026-05-27

Thay đổi:
- Tạo bảng `procedure_fees` lưu lệ phí theo từng phương thức nộp hồ sơ
  (Trực tiếp / Trực tuyến / Dịch vụ bưu chính × nhiều tier giá)
- Mỗi procedure có 0..N fee rows, FK đến procedures.id
- Cột `procedures.fee` (VARCHAR 500) giữ nguyên để denormalize summary
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f3c1a2b4d8e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "procedure_fees",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "procedure_id",
            sa.String(36),
            sa.ForeignKey("procedures.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("submission_method", sa.String(100), nullable=False, index=True),
        sa.Column("processing_time", sa.String(200), nullable=True),
        sa.Column("amount_text", sa.String(300), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("order", sa.Integer, nullable=False, server_default="0"),
        mysql_charset="utf8mb4",
        # Phải khớp với collation của bảng `procedures` (utf8mb4_0900_ai_ci),
        # nếu không MySQL sẽ từ chối FK với lỗi 3780 (incompatible columns).
        mysql_collate="utf8mb4_0900_ai_ci",
    )


def downgrade() -> None:
    op.drop_table("procedure_fees")
