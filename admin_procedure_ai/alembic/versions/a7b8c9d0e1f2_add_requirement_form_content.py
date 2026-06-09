"""add form content + fields parsed for procedure_requirements

Revision ID: a7b8c9d0e1f2
Revises: e5f6a7b8c9d0
Create Date: 2026-06-07

Phase 11 — AI hướng dẫn điền biểu mẫu.
Lưu kết quả parse file biểu mẫu (DOCX/PDF/XLS) vào DB lúc crawl. Click nút
"Hướng dẫn điền" trên FE → LLM sinh hướng dẫn từ form_content_text +
form_fields_json + tình huống user.

Cột mới:
  - form_content_text: TEXT — raw text extract từ file (giới hạn ~5KB)
  - form_fields_json:  JSON — [{label, hint, required}] đã detect
  - form_parsed_at:    DATETIME — lần parse cuối (re-parse khi đổi URL)
  - form_parse_status: VARCHAR(20) — 'ok' | 'failed' | 'unsupported' | NULL
                       NULL = chưa parse → backfill query nhanh qua index
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "procedure_requirements",
        sa.Column("form_content_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "procedure_requirements",
        sa.Column("form_fields_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "procedure_requirements",
        sa.Column("form_parsed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "procedure_requirements",
        sa.Column("form_parse_status", sa.String(20), nullable=True),
    )
    op.create_index(
        "ix_procedure_requirements_form_parse_status",
        "procedure_requirements",
        ["form_parse_status"],
    )
    # Mở rộng messages.section_type để chứa "form_guide:{requirement_id}"
    # (UUID 36 ký tự + prefix → cần ~50). Cũ là VARCHAR(30) cho 6 section
    # types đơn giản (steps/requirements/...). VARCHAR(60) đủ buffer.
    op.alter_column(
        "messages",
        "section_type",
        existing_type=sa.String(30),
        type_=sa.String(60),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "messages",
        "section_type",
        existing_type=sa.String(60),
        type_=sa.String(30),
        existing_nullable=True,
    )
    op.drop_index(
        "ix_procedure_requirements_form_parse_status",
        table_name="procedure_requirements",
    )
    op.drop_column("procedure_requirements", "form_parse_status")
    op.drop_column("procedure_requirements", "form_parsed_at")
    op.drop_column("procedure_requirements", "form_fields_json")
    op.drop_column("procedure_requirements", "form_content_text")
