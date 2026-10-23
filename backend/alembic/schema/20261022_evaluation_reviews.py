"""Append-only competitive question eligibility decisions."""

import sqlalchemy as sa
from alembic import op

revision = "evaluation_reviews_20261022"
down_revision = "intelligence_worker_20261016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "intelligence_evaluation_reviews",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("audit_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base_hash", sa.String(64), nullable=False),
        sa.Column("cohort", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("project_id", "audit_id", "version"),
        sa.UniqueConstraint("project_id", "idempotency_key"),
    )
    op.create_index("ix_intelligence_evaluation_reviews_project_id", "intelligence_evaluation_reviews", ["project_id"])


def downgrade():
    raise RuntimeError("Removing audit review history requires explicit authorization")
