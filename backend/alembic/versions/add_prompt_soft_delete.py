"""add_prompt_soft_delete

Revision ID: add_prompt_soft_delete
Revises: 3550cd9ef734
Create Date: 2026-06-12

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_prompt_soft_delete'
down_revision: Union[str, Sequence[str], None] = '3550cd9ef734'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add soft delete support to prompts table.

    - Add deleted_at TIMESTAMP column (nullable)
    - Add index for efficient filtering of active prompts
    """
    op.add_column(
        "prompts",
        sa.Column("deleted_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_prompts_deleted_at", "prompts", ["deleted_at"])


def downgrade() -> None:
    """Remove soft delete support from prompts table."""
    op.drop_index("ix_prompts_deleted_at", "prompts")
    op.drop_column("prompts", "deleted_at")
