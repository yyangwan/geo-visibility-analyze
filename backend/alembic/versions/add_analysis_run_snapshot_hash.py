"""add analysis run id and source snapshot hash

Revision ID: add_analysis_run_snapshot_hash
Revises: add_platform_raw
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_analysis_run_snapshot_hash"
down_revision: Union[str, Sequence[str], None] = "add_platform_raw"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audits",
        sa.Column("analysis_run_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_audits_analysis_run_id",
        "audits",
        ["analysis_run_id"],
        unique=True,
    )

    op.add_column(
        "platform_response_records",
        sa.Column("source_snapshot_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("platform_response_records", "source_snapshot_hash")
    op.drop_index("ix_audits_analysis_run_id", table_name="audits")
    op.drop_column("audits", "analysis_run_id")
