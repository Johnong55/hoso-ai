"""add crawl_diff_logs table

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-06-17

Lưu kết quả so sánh giữa dữ liệu trước và sau mỗi lần crawl 1 nguồn
(thêm mới / cập nhật / xóa thủ tục). Phục vụ tính năng tự động crawl
theo lịch (weekly/monthly) + admin theo dõi thay đổi.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "crawl_diff_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(36),
            sa.ForeignKey("document_sources.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "run_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
            index=True,
        ),
        sa.Column("added_count", sa.Integer, default=0, nullable=False),
        sa.Column("updated_count", sa.Integer, default=0, nullable=False),
        sa.Column("removed_count", sa.Integer, default=0, nullable=False),
        sa.Column("total_after", sa.Integer, default=0, nullable=False),
        sa.Column("added_codes", sa.JSON, nullable=True),
        sa.Column("updated_codes", sa.JSON, nullable=True),
        sa.Column("removed_codes", sa.JSON, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("crawl_diff_logs")
