"""Remove User/Project/Brand tables, change FKs to string

Revision ID: remove_user_project_brand
Revises: add_suggestion_detail
Create Date: 2026-06-01

Since there is no production data, this migration drops and recreates
the relevant columns. Foreign key constraint names are auto-detected.
"""
from alembic import op
import sqlalchemy as sa

revision = 'remove_user_project_brand'
down_revision = 'add_suggestion_detail'
branch_labels = None
depends_on = None


def _drop_fkIfExists(table: str, column: str) -> None:
    """Drop any FK constraint on table.column, ignoring if not found."""
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT CONSTRAINT_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
        "AND COLUMN_NAME = :column AND REFERENCED_TABLE_NAME IS NOT NULL"
    ), {"table": table, "column": column})
    for row in result:
        op.drop_constraint(row[0], table, type_='foreignkey')


def upgrade() -> None:
    # Drop all FK constraints pointing to projects, brands, or users
    _drop_fkIfExists('query_results', 'brand_id')
    _drop_fkIfExists('prompts', 'project_id')
    _drop_fkIfExists('audits', 'project_id')
    _drop_fkIfExists('reports', 'project_id')
    _drop_fkIfExists('suggestions', 'project_id')
    _drop_fkIfExists('scheduled_jobs', 'project_id')
    _drop_fkIfExists('source_citations', 'project_id')
    _drop_fkIfExists('projects', 'user_id')

    # Drop tables
    op.drop_table('brands')
    op.drop_table('projects')
    op.drop_table('users')

    # Change project_id columns from integer to varchar(50)
    for table in ['prompts', 'audits', 'reports', 'suggestions', 'scheduled_jobs', 'source_citations']:
        op.alter_column(table, 'project_id', existing_type=sa.Integer(), type_=sa.String(50))

    # Change brand_id in query_results from integer to varchar(50)
    op.alter_column('query_results', 'brand_id', existing_type=sa.Integer(), type_=sa.String(50))

    # Add brands_json column to audits
    op.add_column('audits', sa.Column('brands_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    raise NotImplementedError('Downgrade not supported for this migration')
