import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402


engine = create_engine(settings.database_url.replace("+aiomysql", "+pymysql"))
with engine.connect() as conn:
    rows = conn.execute(
        text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND COLUMN_NAME IN ('project_id', 'brand_id') "
            "ORDER BY TABLE_NAME, COLUMN_NAME"
        )
    ).fetchall()

for table, column, column_type in rows:
    print(f"{table}.{column}={column_type}")

engine.dispose()
