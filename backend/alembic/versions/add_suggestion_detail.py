"""Add detail JSON column to suggestions table.

Revision ID: add_suggestion_detail
Revises: add_genilink_user_id
Create Date: 2026-05-12
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = 'add_suggestion_detail'
down_revision = 'add_genilink_user_id'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('suggestions', sa.Column('detail', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('suggestions', 'detail')
