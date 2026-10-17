"""Independent worker liveness, separate from task leases."""

from alembic import op
import sqlalchemy as sa

revision = "intelligence_worker_20261016"
down_revision = "intelligence_baseline_20261014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "intelligence_worker_heartbeats",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("storage_status", sa.String(24), nullable=False),
        sa.Column("storage_checked_at", sa.DateTime(), nullable=True),
        sa.Column("disk_status", sa.String(16), nullable=False),
        sa.Column("disk_checked_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_intelligence_worker_heartbeats_last_seen_at",
        "intelligence_worker_heartbeats",
        ["last_seen_at"],
    )


def downgrade():
    raise RuntimeError("Retiring operational records requires explicit authorization")
