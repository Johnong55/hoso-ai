"""add_case_group_fix_chunk_type_enum

Revision ID: f3c1a2b4d8e0
Revises: d9a0fa4f5ef6
Create Date: 2026-05-10

- procedure_requirements: thêm cột case_group VARCHAR(500)
  → lưu tên trường hợp hồ sơ để group requirements khi chunking
- document_chunks.chunk_type: thêm 'result', 'legal_basis'
  → đồng bộ với Python ChunkType enum
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f3c1a2b4d8e0'
down_revision: Union[str, None] = 'd9a0fa4f5ef6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "procedure_requirements",
        sa.Column("case_group", sa.String(500), nullable=True),
    )
    op.execute(
        "ALTER TABLE document_chunks MODIFY COLUMN chunk_type "
        "ENUM('general','requirement','step','fee','deadline','form','result','legal_basis') NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("procedure_requirements", "case_group")
    op.execute(
        "ALTER TABLE document_chunks MODIFY COLUMN chunk_type "
        "ENUM('general','requirement','step','fee','deadline','form') NOT NULL"
    )
