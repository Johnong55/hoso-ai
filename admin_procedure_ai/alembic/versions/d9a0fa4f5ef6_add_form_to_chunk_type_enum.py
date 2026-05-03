"""add_form_to_chunk_type_enum

Revision ID: d9a0fa4f5ef6
Revises: 8b38e1fc4b0f
Create Date: 2026-05-02 10:11:37.669402

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd9a0fa4f5ef6'
down_revision: Union[str, None] = '8b38e1fc4b0f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE document_chunks MODIFY COLUMN chunk_type "
        "ENUM('general','requirement','step','fee','deadline','form') NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE document_chunks MODIFY COLUMN chunk_type "
        "ENUM('general','requirement','step','fee','deadline') NOT NULL"
    )
