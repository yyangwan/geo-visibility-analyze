"""Check lease timing and advisory locks against a dedicated PostgreSQL test DB."""

import asyncio
import sys

from sqlalchemy import create_engine

from app.database import async_session, engine, sync_database_url
from app.intelligence.queue import db_now
from app.intelligence.schema_lock import SchemaLockUnavailable, schema_lock


def check_lock():
    sync_engine = create_engine(sync_database_url())
    try:
        with sync_engine.connect() as first, sync_engine.connect() as second:
            with schema_lock(first):
                try:
                    with schema_lock(second, timeout=0):
                        raise AssertionError("Concurrent schema mutex acquisition")
                except SchemaLockUnavailable:
                    pass
            with schema_lock(second, timeout=0):
                pass
    finally:
        sync_engine.dispose()


async def check_clock():
    try:
        async with async_session() as db, db.begin():
            earlier = await db_now(db)
            await asyncio.sleep(0.05)
            assert await db_now(db) > earlier
    finally:
        await engine.dispose()


if __name__ == "__main__":
    url = sync_database_url()
    if url.drivername != "postgresql+psycopg" or url.database not in {
        "analysis_pg_ci", "analysis_pg_local"
    } or url.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("A dedicated local PostgreSQL test database is required")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    check_lock()
    asyncio.run(check_clock())
    print("PostgreSQL advisory lock and lease timing verified")
