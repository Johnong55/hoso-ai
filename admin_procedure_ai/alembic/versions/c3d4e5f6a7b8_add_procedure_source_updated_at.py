"""add procedure source_updated_at for API change detection

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-02

Thêm cột `procedures.source_updated_at` (BIGINT, epoch milliseconds) chứa
`updatedAt` lấy trực tiếp từ JSON API DVCQG mới. Khi re-crawl chỉ cần so
sánh giá trị này thay vì phải tải + parse full detail rồi tính hash —
tiết kiệm cả HTTP và embedding quota.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "procedures",
        sa.Column("source_updated_at", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_procedures_source_updated_at",
        "procedures",
        ["source_updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_procedures_source_updated_at", table_name="procedures")
    op.drop_column("procedures", "source_updated_at")
