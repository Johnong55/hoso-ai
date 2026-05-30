"""add procedure content_hash for change detection

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-30

Thêm cột `procedures.content_hash` (SHA256 hex của parsed content) để khi re-crawl
có thể compare hash → skip embed nếu nội dung không đổi → tiết kiệm quota API.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "procedures",
        sa.Column("content_hash", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_procedures_content_hash",
        "procedures",
        ["content_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_procedures_content_hash", table_name="procedures")
    op.drop_column("procedures", "content_hash")
