"""Generate the reviewed fresh-install baseline; never execute it on a database.

Run from backend with AISCOPE_DEBUG=1. Old schema snapshots remain archived and outside the active chain.
The generated file contains static operations and does not import application models.
"""

from pathlib import Path

from alembic.autogenerate import render_python_code
from alembic.migration import MigrationContext
from alembic.operations.ops import CreateIndexOp, CreateTableOp, UpgradeOps
from sqlalchemy.dialects import mysql

from app.database import Base
from app.intelligence import models as intelligence_models  # noqa: F401
from app.intelligence.schema_version import SCHEMA_REVISION
from app.models import device_gateway, models  # noqa: F401

operations = []
for table in Base.metadata.sorted_tables:
    operations.append(CreateTableOp.from_table(table))
    operations.extend(
        CreateIndexOp.from_index(index)
        for index in sorted(table.indexes, key=lambda item: item.name)
    )
body = render_python_code(
    UpgradeOps(operations),
    migration_context=MigrationContext.configure(dialect=mysql.dialect()),
)
target = Path("alembic/value_schema/20261024_value_baseline.py")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(
    f'''"""Fresh-install baseline. No existing-database upgrade or deletion is performed."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "{SCHEMA_REVISION}"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names()) - {{"alembic_version"}}
    if existing:
        raise RuntimeError("Fresh initialization requires an empty database; no existing data is upgraded or removed")
'''
    + body
    + '\n\ndef downgrade():\n    raise RuntimeError("Baseline downgrade requires an explicitly authorized database retirement")\n',
    encoding="utf-8",
)
print(f"Frozen baseline in {target}; archived schema snapshots are unchanged")
