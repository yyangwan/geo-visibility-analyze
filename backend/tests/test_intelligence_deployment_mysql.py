"""The real MySQL predicate cannot accept a previous deployment's heartbeat."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, text, update

from app.intelligence.models import WorkerHeartbeat
from scripts.check_intelligence_deployment import worker_ready
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401

pytestmark = pytest.mark.skipif(not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured")


@pytest.mark.asyncio
async def test_worker_acceptance_requires_a_new_live_verified_heartbeat(
    mysql_sessions,  # noqa: F811 - imported isolated fixture
):
    _, engine = mysql_sessions
    # All rows live in one rolled-back transaction; no project/worker data is removed.
    async with engine.connect() as connection, connection.begin():
        now = (await connection.execute(text("SELECT CURRENT_TIMESTAMP"))).scalar_one()
        since = now - timedelta(seconds=10)
        old_id, new_id, unrelated_id = uuid4().hex, uuid4().hex, uuid4().hex
        values = {"last_seen_at": now, "state": "idle", "storage_status": "configuration_verified", "disk_status": "ok", "storage_checked_at": now, "disk_checked_at": now}
        await connection.execute(insert(WorkerHeartbeat).values(id=old_id, started_at=since, **values))
        assert not await connection.run_sync(lambda db: worker_ready(db, since, new_id))
        # A different deployment may restart after our watermark. Its healthy
        # fresh row must not stand in for the candidate's missing monitor ID.
        await connection.execute(insert(WorkerHeartbeat).values(id=unrelated_id, started_at=since + timedelta(seconds=1), **values))
        assert await connection.run_sync(lambda db: worker_ready(db, since, unrelated_id))
        assert not await connection.run_sync(lambda db: worker_ready(db, since, new_id))
        await connection.execute(insert(WorkerHeartbeat).values(id=new_id, started_at=since + timedelta(seconds=1), **{**values, "storage_status": "unknown"}))
        assert not await connection.run_sync(lambda db: worker_ready(db, since, new_id))
        await connection.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.id == new_id).values(storage_status="configuration_verified", disk_status="low"))
        assert not await connection.run_sync(lambda db: worker_ready(db, since, new_id))
        await connection.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.id == new_id).values(disk_status="ok", last_seen_at=now - timedelta(seconds=60)))
        assert not await connection.run_sync(lambda db: worker_ready(db, since, new_id))
        await connection.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.id == new_id).values(last_seen_at=now, state="stopped"))
        assert not await connection.run_sync(lambda db: worker_ready(db, since, new_id))
        await connection.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.id == new_id).values(state="working"))
        assert await connection.run_sync(lambda db: worker_ready(db, since, new_id))
        # Explicit rollback keeps this acceptance test isolated from all other cases.
        await connection.rollback()
