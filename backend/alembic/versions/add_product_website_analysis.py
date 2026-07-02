"""Add product website analysis tables.

Revision ID: add_product_website_analysis
Revises: add_analysis_run_snapshot_hash
Create Date: 2026-06-30 12:30:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_product_website_analysis"
down_revision: Union[str, Sequence[str], None] = "add_analysis_run_snapshot_hash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_website_analyses",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("workspace_id", sa.String(length=50), nullable=False),
        sa.Column("project_id", sa.String(length=50), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("normalized_domain", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(length=50), nullable=False, server_default="queued"),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("input_snapshot", sa.JSON(), nullable=True),
        sa.Column("result_snapshot", sa.JSON(), nullable=True),
        sa.Column("score_overall", sa.Float(), nullable=True),
        sa.Column("score_grade", sa.String(length=10), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_pwa_project_id", "product_website_analyses", ["project_id"])
    op.create_index("ix_pwa_workspace_id", "product_website_analyses", ["workspace_id"])
    op.create_index("ix_pwa_project_created", "product_website_analyses", ["project_id", "created_at"])

    op.create_table(
        "product_website_stage_runs",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("analysis_id", sa.Integer(), sa.ForeignKey("product_website_analyses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage_name", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("input_snapshot", sa.JSON(), nullable=True),
        sa.Column("output_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_pwsr_analysis_id", "product_website_stage_runs", ["analysis_id"])
    op.create_index("ix_pwsr_analysis_stage", "product_website_stage_runs", ["analysis_id", "stage_name"])

    op.create_table(
        "product_website_crawl_logs",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("analysis_id", sa.Integer(), sa.ForeignKey("product_website_analyses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("method", sa.String(length=50), nullable=False, server_default="native_fetch"),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("content_length", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_pwcl_analysis_id", "product_website_crawl_logs", ["analysis_id"])

    op.create_table(
        "product_website_events",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("analysis_id", sa.Integer(), sa.ForeignKey("product_website_analyses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("stage", sa.String(length=50), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_pwe_analysis_id", "product_website_events", ["analysis_id"])
    op.create_index("ix_pwe_created_at", "product_website_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_pwe_created_at", table_name="product_website_events")
    op.drop_index("ix_pwe_analysis_id", table_name="product_website_events")
    op.drop_table("product_website_events")
    op.drop_index("ix_pwcl_analysis_id", table_name="product_website_crawl_logs")
    op.drop_table("product_website_crawl_logs")
    op.drop_index("ix_pwsr_analysis_stage", table_name="product_website_stage_runs")
    op.drop_index("ix_pwsr_analysis_id", table_name="product_website_stage_runs")
    op.drop_table("product_website_stage_runs")
    op.drop_index("ix_pwa_project_created", table_name="product_website_analyses")
    op.drop_index("ix_pwa_workspace_id", table_name="product_website_analyses")
    op.drop_index("ix_pwa_project_id", table_name="product_website_analyses")
    op.drop_table("product_website_analyses")
