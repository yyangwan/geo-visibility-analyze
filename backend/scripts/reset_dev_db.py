import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models.models  # noqa: F401 - register models with Base
from app.database import Base, engine


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        result = await conn.execute(
            text(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE()"
            )
        )
        tables = result.scalars().all()
        for table in tables:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        await conn.run_sync(Base.metadata.create_all)

    await engine.dispose()
    print(f"CREATED_TABLES={len(Base.metadata.tables)}")


if __name__ == "__main__":
    asyncio.run(main())
