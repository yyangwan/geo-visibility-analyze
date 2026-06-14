"""add_platform_configs_and_raw_response

Revision ID: add_platform_raw
Revises: add_prompt_soft_delete
Create Date: 2026-06-13

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_platform_raw'
down_revision: Union[str, Sequence[str], None] = 'add_prompt_soft_delete'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add platform configuration and raw response storage.

    Issue 1.1: Create platform_configs table
    - Stores platform-specific search parameters, parsing rules, and metadata
    - Supports versioning and rollback for configuration changes

    Issue 1.2: Extend platform_response_records table
    - Add raw_response and raw_response_text for complete response archiving
    - Add search_metadata for search query and reasoning tracking
    - Add request_params for request parameter storage
    - Add parse_error for graceful error handling
    """

    # Issue 1.1: Create platform_configs table
    op.create_table(
        "platform_configs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("platform", sa.String(50), nullable=False, unique=True),
        sa.Column("config_version", sa.Integer, default=1, nullable=False),
        sa.Column("config_json", sa.JSON, nullable=False, default=dict),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Index("ix_platform_configs_platform", "platform"),
        sa.Index("ix_platform_configs_is_active", "is_active"),
    )

    # Issue 1.2: Extend platform_response_records table
    op.add_column(
        "platform_response_records",
        sa.Column("raw_response", sa.JSON, nullable=True),
    )
    op.add_column(
        "platform_response_records",
        sa.Column("raw_response_text", sa.Text, nullable=True),
    )
    op.add_column(
        "platform_response_records",
        sa.Column("search_metadata", sa.JSON, nullable=True),
    )
    op.add_column(
        "platform_response_records",
        sa.Column("request_params", sa.JSON, nullable=True),
    )
    op.add_column(
        "platform_response_records",
        sa.Column("parse_error", sa.Text, nullable=True),
    )


def downgrade() -> None:
    """Remove platform configuration and raw response storage."""

    # Issue 1.2: Remove extended columns from platform_response_records
    op.drop_column("platform_response_records", "parse_error")
    op.drop_column("platform_response_records", "request_params")
    op.drop_column("platform_response_records", "search_metadata")
    op.drop_column("platform_response_records", "raw_response_text")
    op.drop_column("platform_response_records", "raw_response")

    # Issue 1.1: Drop platform_configs table
    op.drop_index("ix_platform_configs_is_active", "platform_configs")
    op.drop_index("ix_platform_configs_platform", "platform_configs")
    op.drop_table("platform_configs")
