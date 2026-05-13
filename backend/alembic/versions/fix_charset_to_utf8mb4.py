"""Fix charset to utf8mb4 for Phase 1 tables.

Revision ID: fix_charset_utf8mb4
Revises: add_response_analyses
Create Date: 2026-05-11
"""

from alembic import op

# revision identifiers
revision = 'fix_charset_utf8mb4'
down_revision = 'add_response_analyses'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Convert Phase 1 tables from utf8 to utf8mb4 for emoji support."""
    op.execute(
        'ALTER TABLE platform_response_records '
        'CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'
    )
    op.execute(
        'ALTER TABLE source_citations '
        'CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'
    )
    op.execute(
        'ALTER TABLE response_analyses '
        'CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'
    )


def downgrade() -> None:
    """No-op — we don't want to go back to utf8."""
    pass
