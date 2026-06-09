"""Add persisted audit workflow tables and state columns.

Revision ID: b2b7f8c5d9a1
Revises: remove_user_project_brand
Create Date: 2026-06-08 21:20:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2b7f8c5d9a1"
down_revision: Union[str, Sequence[str], None] = "remove_user_project_brand"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


AUDIT_STAGE_VALUES = (
    "queued",
    "querying",
    "persisting",
    "calculating",
    "finalizing",
    "completed",
    "partial",
    "failed",
    "stalled",
)

RUN_STATUS_VALUES = (
    "pending",
    "running",
    "completed",
    "failed",
    "retrying",
)


def upgrade() -> None:
    op.add_column(
        "audits",
        sa.Column(
            "stage",
            sa.Enum(*AUDIT_STAGE_VALUES),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
    )
    op.add_column(
        "audits",
        sa.Column(
            "stage_status",
            sa.Enum(*RUN_STATUS_VALUES),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column(
        "audits",
        sa.Column("stage_started_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "audits",
        sa.Column("stage_updated_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "audits",
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "audits",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "audits",
        sa.Column("error_code", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "audits",
        sa.Column("recoverable_error", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "audits",
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "audits",
        sa.Column("locked_by_worker", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "audits",
        sa.Column("locked_until", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "audit_stage_runs",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("audit_id", sa.Integer(), sa.ForeignKey("audits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage_name", sa.String(length=50), nullable=False),
        sa.Column("status", sa.Enum(*RUN_STATUS_VALUES), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("input_snapshot", sa.JSON(), nullable=True),
        sa.Column("output_snapshot", sa.JSON(), nullable=True),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("audit_id", "stage_name", "attempt_no", name="uq_asr_audit_stage_attempt"),
    )
    op.create_index("ix_asr_audit_id", "audit_stage_runs", ["audit_id"])
    op.create_index("ix_asr_audit_stage", "audit_stage_runs", ["audit_id", "stage_name"])
    op.create_index("ix_asr_status", "audit_stage_runs", ["status"])

    op.create_table(
        "audit_platform_runs",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("audit_id", sa.Integer(), sa.ForeignKey("audits.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "stage_run_id",
            sa.Integer(),
            sa.ForeignKey("audit_stage_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("prompt_id", sa.Integer(), sa.ForeignKey("prompts.id"), nullable=False),
        sa.Column("platform", sa.String(length=50), nullable=False),
        sa.Column("status", sa.Enum(*RUN_STATUS_VALUES), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "response_record_id",
            sa.Integer(),
            sa.ForeignKey("platform_response_records.id", ondelete="SET NULL"),
            nullable=True,
            unique=True,
        ),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("retry_after", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("audit_id", "prompt_id", "platform", "attempt_no", name="uq_apr_audit_prompt_platform_attempt"),
    )
    op.create_index("ix_apr_audit_id", "audit_platform_runs", ["audit_id"])
    op.create_index("ix_apr_audit_platform", "audit_platform_runs", ["audit_id", "platform"])
    op.create_index("ix_apr_status", "audit_platform_runs", ["status"])

    op.create_table(
        "audit_events_log",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("audit_id", sa.Integer(), sa.ForeignKey("audits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage_name", sa.String(length=50), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_ael_audit_id", "audit_events_log", ["audit_id"])
    op.create_index("ix_ael_stage_name", "audit_events_log", ["stage_name"])
    op.create_index("ix_ael_created_at", "audit_events_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ael_created_at", table_name="audit_events_log")
    op.drop_index("ix_ael_stage_name", table_name="audit_events_log")
    op.drop_index("ix_ael_audit_id", table_name="audit_events_log")
    op.drop_table("audit_events_log")

    op.drop_index("ix_apr_status", table_name="audit_platform_runs")
    op.drop_index("ix_apr_audit_platform", table_name="audit_platform_runs")
    op.drop_index("ix_apr_audit_id", table_name="audit_platform_runs")
    op.drop_table("audit_platform_runs")

    op.drop_index("ix_asr_status", table_name="audit_stage_runs")
    op.drop_index("ix_asr_audit_stage", table_name="audit_stage_runs")
    op.drop_index("ix_asr_audit_id", table_name="audit_stage_runs")
    op.drop_table("audit_stage_runs")

    op.drop_column("audits", "locked_until")
    op.drop_column("audits", "locked_by_worker")
    op.drop_column("audits", "next_retry_at")
    op.drop_column("audits", "recoverable_error")
    op.drop_column("audits", "error_code")
    op.drop_column("audits", "attempt_count")
    op.drop_column("audits", "last_heartbeat_at")
    op.drop_column("audits", "stage_updated_at")
    op.drop_column("audits", "stage_started_at")
    op.drop_column("audits", "stage_status")
    op.drop_column("audits", "stage")
