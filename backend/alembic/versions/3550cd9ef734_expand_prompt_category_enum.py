"""expand_prompt_category_enum

Revision ID: 3550cd9ef734
Revises: b2b7f8c5d9a1
Create Date: 2026-06-09 21:55:41.012188

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3550cd9ef734'
down_revision: Union[str, Sequence[str], None] = 'b2b7f8c5d9a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Expand prompt.category enum with new intent categories.

    MySQL ENUM type modification requires ALTER TABLE MODIFY COLUMN.
    We need to redefine the enum with all existing + new values.
    """
    # Get the bind to determine database type
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == "mysql":
        # MySQL ENUM: need to redefine with all values
        op.execute(
            "ALTER TABLE prompts "
            "MODIFY COLUMN category ENUM("
            "'recommend', 'compare', 'evaluate', 'scenario', "
            "'problem_solution', 'alternative_finding', 'decision_help', 'regret_avoidance', "
            "'performance_specs'"
            ") "
            "GENERATED ALWAYS AS (category) STORED"
        )
    elif dialect_name == "postgresql":
        # PostgreSQL ENUM: create new type, migrate data, drop old type
        op.execute("CREATE TYPE prompt_category_new AS ENUM ("
                   "'recommend', 'compare', 'evaluate', 'scenario', "
                   "'problem_solution', 'alternative_finding', 'decision_help', 'regret_avoidance', "
                   "'performance_specs')")
        op.execute("ALTER TABLE prompts ALTER COLUMN category TYPE prompt_category_new USING category::text::prompt_category_new")
        op.execute("DROP TYPE prompt_category")
        op.execute("ALTER TYPE prompt_category_new RENAME TO prompt_category")
    else:
        # SQLite: enum is just CHECK constraint, no migration needed
        pass


def downgrade() -> None:
    """Revert to original 4 prompt categories."""
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == "mysql":
        op.execute(
            "ALTER TABLE prompts "
            "MODIFY COLUMN category ENUM("
            "'recommend', 'compare', 'evaluate', 'scenario'"
            ") "
            "GENERATED ALWAYS AS (category) STORED"
        )
    elif dialect_name == "postgresql":
        op.execute("CREATE TYPE prompt_category_old AS ENUM ('recommend', 'compare', 'evaluate', 'scenario')")
        op.execute("ALTER TABLE prompts ALTER COLUMN category TYPE prompt_category_old USING category::text::prompt_category_old")
        op.execute("DROP TYPE prompt_category")
        op.execute("ALTER TYPE prompt_category_old RENAME TO prompt_category")
    else:
        pass
