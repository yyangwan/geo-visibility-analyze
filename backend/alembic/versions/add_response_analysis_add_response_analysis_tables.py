"""Add PlatformResponseRecord, SourceCitation, and QR FK

Revision ID: add_response_analysis
Revises: 918c19406f61
Create Date: 2026-05-11 08:13:23.771714

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_response_analysis'
down_revision: Union[str, Sequence[str], None] = '918c19406f61'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add platform_response_records, source_citations tables and QR FK."""
    # 1. Create platform_response_records (must come before query_results FK)
    op.create_table(
        'platform_response_records',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('audit_id', sa.Integer(), sa.ForeignKey('audits.id', ondelete='CASCADE'), nullable=False),
        sa.Column('prompt_id', sa.Integer(), sa.ForeignKey('prompts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('platform', sa.String(50), nullable=False),
        sa.Column('response_text', sa.Text(), nullable=True),
        sa.Column('citations', sa.JSON(), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('completion_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('response_model', sa.String(100), server_default='', nullable=False),
        sa.Column('finish_reason', sa.String(20), server_default='', nullable=False),
        sa.Column('search_enabled', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_prr_audit_id', 'platform_response_records', ['audit_id'])
    op.create_index('ix_prr_unique', 'platform_response_records',
                     ['audit_id', 'prompt_id', 'platform'], unique=True)

    # 2. Add response_record_id FK to query_results
    op.add_column('query_results',
        sa.Column('response_record_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_query_results_prr', 'query_results', 'platform_response_records',
        ['response_record_id'], ['id'], ondelete='SET NULL')

    # 3. Create source_citations
    op.create_table(
        'source_citations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('project_id', sa.String(50), nullable=False),
        sa.Column('audit_id', sa.Integer(), sa.ForeignKey('audits.id', ondelete='SET NULL'), nullable=True),
        sa.Column('domain', sa.String(200), nullable=False),
        sa.Column('urls', sa.JSON(), nullable=True),
        sa.Column('citation_count', sa.Integer(), server_default='1', nullable=False),
        sa.Column('platform', sa.String(50), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_source_domain', 'source_citations', ['domain'])
    op.create_index('ix_source_project_audit', 'source_citations', ['project_id', 'audit_id'])
    op.create_index('ix_source_unique', 'source_citations',
                     ['project_id', 'audit_id', 'domain', 'platform'], unique=True)


def downgrade() -> None:
    """Remove source_citations, QR FK, and platform_response_records."""
    # Drop in reverse order
    op.drop_index('ix_source_unique', table_name='source_citations')
    op.drop_index('ix_source_project_audit', table_name='source_citations')
    op.drop_index('ix_source_domain', table_name='source_citations')
    op.drop_table('source_citations')

    op.drop_constraint('fk_query_results_prr', 'query_results', type_='foreignkey')
    op.drop_column('query_results', 'response_record_id')

    op.drop_index('ix_prr_unique', table_name='platform_response_records')
    op.drop_index('ix_prr_audit_id', table_name='platform_response_records')
    op.drop_table('platform_response_records')
