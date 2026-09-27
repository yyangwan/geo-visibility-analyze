"""Add idempotent product website analysis requests.

Revision ID: add_product_website_idempotency
Revises: add_device_gateway_tasks
Create Date: 2026-09-26 10:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_product_website_idempotency"
down_revision: Union[str, Sequence[str], None] = "add_device_gateway_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "product_website_analyses",
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "product_website_analyses",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_pwa_project_idempotency",
        "product_website_analyses",
        ["project_id", "idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_pwa_project_idempotency", table_name="product_website_analyses")
    op.drop_column("product_website_analyses", "request_fingerprint")
    op.drop_column("product_website_analyses", "idempotency_key")
