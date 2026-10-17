"""Generate the reviewed fresh-install baseline; never execute it on a database.

Run from backend with AISCOPE_DEBUG=1. Old revisions remain archived in versions/.
The generated file contains static operations and does not import application models.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic.autogenerate import render_python_code
from alembic.migration import MigrationContext
from alembic.operations.ops import CreateIndexOp, CreateTableOp, UpgradeOps
from sqlalchemy.dialects import mysql

from app.database import Base
from app.models import models, device_gateway
from app.intelligence import models as intelligence_models

operations = []
for table in Base.metadata.sorted_tables:
    # Added by a separate reviewed revision; do not duplicate it in the baseline.
    if table.name == "intelligence_worker_heartbeats":
        continue
    operations.append(CreateTableOp.from_table(table))
    operations.extend(CreateIndexOp.from_index(index) for index in sorted(table.indexes, key=lambda item: item.name))
body = render_python_code(UpgradeOps(operations), migration_context=MigrationContext.configure(dialect=mysql.dialect()))
target = Path("alembic/schema/20261014_intelligence_baseline.py")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text('''"""Fresh-install baseline. No existing-database upgrade or deletion is performed."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "intelligence_baseline_20261014"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
''' + body + '\n\ndef downgrade():\n    raise RuntimeError("Baseline downgrade requires an explicitly authorized database retirement")\n', encoding="utf-8")
print(f"Frozen baseline in {target}; extension revisions remain separate")
