"""Add ResponseAnalysis table

Revision ID: add_response_analyses
Revises: add_response_analysis
Create Date: 2026-05-11 08:28:42.304453

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_response_analyses'
down_revision: Union[str, Sequence[str], None] = 'add_response_analysis'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add response_analyses table."""
    op.create_table(
        'response_analyses',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('response_record_id', sa.Integer(),
                  sa.ForeignKey('platform_response_records.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('cited_sources', sa.JSON(), nullable=True),
        sa.Column('brand_sentiment', sa.String(20), nullable=True),
        sa.Column('brand_attributes', sa.JSON(), nullable=True),
        sa.Column('topics_covered', sa.JSON(), nullable=True),
        sa.Column('answer_structure', sa.String(20), nullable=True),
        sa.Column('competitor_refs', sa.JSON(), nullable=True),
        sa.Column('analysis_model', sa.String(100), server_default='', nullable=False),
        sa.Column('status', sa.String(20), server_default='pending', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_ra_response_record_id', 'response_analyses', ['response_record_id'])


def downgrade() -> None:
    """Remove response_analyses table."""
    op.drop_index('ix_ra_response_record_id', table_name='response_analyses')
    op.drop_table('response_analyses')
