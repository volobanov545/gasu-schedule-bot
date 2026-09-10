"""Add explicit bounded document-extraction state to archived files.

Revision ID: 6f742d8c93a1
Revises: 3c7927bc81df
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from szs_hub.storage.base import UTCDateTime

revision: str = "6f742d8c93a1"
down_revision: str | None = "3c7927bc81df"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.add_column(sa.Column("extraction_status", sa.String(length=32)))
        batch_op.add_column(sa.Column("extraction_error_code", sa.String(length=100)))
        batch_op.add_column(sa.Column("extraction_version", sa.String(length=64)))
        batch_op.add_column(sa.Column("extracted_at", UTCDateTime(timezone=True)))
        batch_op.create_check_constraint(
            "valid_extraction_status",
            "extraction_status IS NULL OR extraction_status IN ("
            "'ok', 'needs_ocr', 'unsupported', 'type_mismatch', 'limit_exceeded', "
            "'password_protected', 'corrupt', 'timeout', 'error')",
        )


def downgrade() -> None:
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.drop_constraint("valid_extraction_status", type_="check")
        batch_op.drop_column("extracted_at")
        batch_op.drop_column("extraction_version")
        batch_op.drop_column("extraction_error_code")
        batch_op.drop_column("extraction_status")
