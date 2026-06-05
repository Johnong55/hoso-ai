"""add procedure formality_id for online submission link

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-04

Thêm cột `procedures.formality_id` lưu UUID gốc của thủ tục bên DVCQG
(field `data.id` từ API detail). Dùng để build URL nộp trực tuyến:
    https://dichvucong.gov.vn/tim-kiem-thu-tuc-hanh-chinh?formalityId=<UUID>

Khác với `procedures.id` (UUID nội bộ do hệ thống mình generate).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "procedures",
        sa.Column("formality_id", sa.String(40), nullable=True),
    )
    op.create_index(
        "ix_procedures_formality_id",
        "procedures",
        ["formality_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_procedures_formality_id", table_name="procedures")
    op.drop_column("procedures", "formality_id")
