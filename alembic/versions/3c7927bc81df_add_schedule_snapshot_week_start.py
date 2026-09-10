"""Add the requested week to immutable schedule snapshot identity.

Revision ID: 3c7927bc81df
Revises: 8e9d7f42c1a0
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "3c7927bc81df"
down_revision: str | None = "8e9d7f42c1a0"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedule_snapshots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("week_start", sa.Date(), nullable=True))

    # There is no production database at this stage. Still preserve development data
    # deterministically: derive Monday from the earliest lesson, or from fetched_at for
    # a historical empty snapshot where no lesson can identify the requested week.
    op.execute(
        """
        UPDATE schedule_snapshots
        SET week_start = date(
            COALESCE(
                (SELECT MIN(lessons.day)
                 FROM lessons
                 WHERE lessons.snapshot_id = schedule_snapshots.id),
                fetched_at
            ),
            '-' || ((CAST(strftime('%w', COALESCE(
                (SELECT MIN(lessons.day)
                 FROM lessons
                 WHERE lessons.snapshot_id = schedule_snapshots.id),
                fetched_at
            )) AS INTEGER) + 6) % 7) || ' days'
        )
        """
    )

    with op.batch_alter_table("schedule_snapshots", schema=None) as batch_op:
        batch_op.alter_column("week_start", existing_type=sa.Date(), nullable=False)
        batch_op.drop_constraint(
            "uq_schedule_snapshots_source_group_key_content_hash",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_schedule_snapshots_source_group_key_week_start_content_hash",
            ["source", "group_key", "week_start", "content_hash"],
        )
        batch_op.drop_index("ix_schedule_snapshots_group_fetched")
        batch_op.create_index(
            "ix_schedule_snapshots_group_week_fetched",
            ["group_key", "week_start", "fetched_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("schedule_snapshots", schema=None) as batch_op:
        batch_op.drop_index("ix_schedule_snapshots_group_week_fetched")
        batch_op.create_index(
            "ix_schedule_snapshots_group_fetched",
            ["group_key", "fetched_at"],
            unique=False,
        )
        batch_op.drop_constraint(
            "uq_schedule_snapshots_source_group_key_week_start_content_hash",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_schedule_snapshots_source_group_key_content_hash",
            ["source", "group_key", "content_hash"],
        )
        batch_op.drop_column("week_start")
