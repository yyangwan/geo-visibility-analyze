"""add genilink_user_id column

Revision ID: add_genilink_user_id
Revises: fix_charset_utf8mb4
Create Date: 2026-05-15
"""

from alembic import op
import sqlalchemy as sa

revision = "add_genilink_user_id"
down_revision = "fix_charset_utf8mb4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("genilink_user_id", sa.String(255), unique=True, nullable=True),
    )
    op.create_index("ix_users_genilink_user_id", "users", ["genilink_user_id"])


def downgrade() -> None:
    op.drop_index("ix_users_genilink_user_id", "users")
    op.drop_column("users", "genilink_user_id")
