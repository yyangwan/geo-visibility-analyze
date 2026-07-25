"""Add outbound device gateway task queue.

Revision ID: add_device_gateway_tasks
Revises: add_product_website_analysis
Create Date: 2026-07-25 11:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_device_gateway_tasks"
down_revision: Union[str, Sequence[str], None] = "add_product_website_analysis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_gateways",
        sa.Column("id", sa.String(length=100), primary_key=True, nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="offline"),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("device_snapshot", sa.JSON(), nullable=False),
        sa.Column("agent_version", sa.String(length=50), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )

    op.create_table(
        "device_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("project_id", sa.String(length=50), nullable=False),
        sa.Column("target_gateway_id", sa.String(length=100), nullable=True),
        sa.Column("gateway_id", sa.String(length=100), nullable=True),
        sa.Column("task_type", sa.String(length=50), nullable=False),
        sa.Column("platform", sa.String(length=50), nullable=True),
        sa.Column("surface", sa.String(length=20), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("idempotency_key", sa.String(length=100), nullable=True),
        sa.Column("available_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["gateway_id"],
            ["device_gateways.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_device_tasks_project_idempotency",
        "device_tasks",
        ["project_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_device_tasks_claim",
        "device_tasks",
        ["status", "target_gateway_id", "available_at", "priority"],
    )
    op.create_index(
        "ix_device_tasks_project_created",
        "device_tasks",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_device_tasks_project_created", table_name="device_tasks")
    op.drop_index("ix_device_tasks_claim", table_name="device_tasks")
    op.drop_index("ix_device_tasks_project_idempotency", table_name="device_tasks")
    op.drop_table("device_tasks")
    op.drop_table("device_gateways")
