"""Real MySQL verification, isolated to an explicitly named local CI database."""

import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.intelligence.models import AnalysisJob
from app.intelligence.queue import JobQueue
from app.intelligence.semantics import stable_hash

MYSQL_URL = os.getenv("AISCOPE_INTELLIGENCE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured"
)


@pytest.mark.asyncio
async def test_schema_mutex_blocks_second_executor_and_releases(mysql_sessions):
    from app.intelligence.schema_lock import SchemaLockUnavailable, schema_lock

    _, engine = mysql_sessions
    async with engine.connect() as first, engine.connect() as second:
        guard = schema_lock(first.sync_connection, timeout=0)
        await first.run_sync(lambda _: guard.__enter__())

        def attempt(connection):
            with schema_lock(connection, timeout=0):
                pass

        try:
            with pytest.raises(SchemaLockUnavailable):
                await second.run_sync(attempt)
        finally:
            await first.run_sync(lambda _: guard.__exit__(None, None, None))
        await second.run_sync(attempt)


@pytest.mark.asyncio
async def test_backup_uses_one_mysql_snapshot_and_checks_object_bytes(
    mysql_sessions, tmp_path
):
    from sqlalchemy import event, insert, select
    from sqlalchemy.util import await_only

    from app.intelligence.backup import export_bundle, unpacked, verify_bundle
    from app.intelligence.blob import LocalBlobStore, digest
    from app.intelligence.models import SourceDocument, SourceSnapshot

    _, engine = mysql_sessions
    project = "backup-ci-" + uuid4().hex
    baseline_id, snapshot_id = uuid4().hex, uuid4().hex
    late_id = uuid4().hex
    triggered = False
    store = LocalBlobStore(tmp_path / "objects")
    key, data = f"projects/{project}/text/body.gz", b"immutable CI body"
    await store.put_immutable(key, data)
    async with engine.begin() as writer:
        await writer.execute(
            insert(SourceDocument).values(
                id=baseline_id,
                project_id=project,
                url="https://example.com/baseline",
                url_hash=uuid4().hex * 2,
                title="baseline",
            )
        )
        await writer.execute(
            insert(SourceSnapshot).values(
                id=snapshot_id,
                project_id=project,
                document_id=baseline_id,
                body_hash="b" * 64,
                representation_hash="r" * 64,
                text_key=key,
                text_sha256=digest(data),
                extractor_version="ci",
            )
        )

    async def insert_after_snapshot():
        async with engine.begin() as writer:
            await writer.execute(
                insert(SourceDocument).values(
                    id=late_id,
                    project_id=project,
                    url="https://example.com/late",
                    url_hash=uuid4().hex * 2,
                    title="late",
                )
            )

    def before_read(_connection, _cursor, statement, _params, _context, _many):
        nonlocal triggered
        if not triggered and statement.startswith("SELECT intelligence_documents."):
            triggered = True
            await_only(insert_after_snapshot())

    event.listen(engine.sync_engine, "before_cursor_execute", before_read)
    target = tmp_path / "mysql-backup"
    try:
        async with engine.connect() as connection:
            result = await export_bundle(connection, store, target)
        assert triggered and verify_bundle(target) == result
        assert result["objects"] == 1
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(SourceDocument.id).where(SourceDocument.id == late_id)
                )
                == late_id
            )
        import json

        exported = [
            json.loads(line)
            for line in (target / "database/intelligence_documents.jsonl")
            .read_text()
            .splitlines()
        ]
        assert all(
            late_id not in [unpacked(value) for value in row] for row in exported
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_read)
        async with engine.begin() as connection:
            await connection.execute(
                SourceSnapshot.__table__.delete().where(
                    SourceSnapshot.id == snapshot_id
                )
            )
            await connection.execute(
                SourceDocument.__table__.delete().where(
                    SourceDocument.id.in_([late_id, baseline_id])
                )
            )


@pytest_asyncio.fixture
async def mysql_sessions():
    parsed = make_url(MYSQL_URL)
    if parsed.host not in ("127.0.0.1", "localhost") or not (
        parsed.database or ""
    ).startswith("intelligence_ci"):
        raise RuntimeError(
            "Refusing to run integration tests against a non-CI database"
        )
    env = dict(
        os.environ, AISCOPE_DATABASE_URL=MYSQL_URL, AISCOPE_SECRET_KEY="ci-only-secret"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        "head",
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if await process.wait() != 0:
        raise RuntimeError(
            "Fresh MySQL baseline migration failed; inspect the CI migration step"
        )
    engine = create_async_engine(
        MYSQL_URL, connect_args={"init_command": "SET time_zone = '+00:00'"}
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False), engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_database_migration_has_all_registered_tables(mysql_sessions):
    _, engine = mysql_sessions
    async with engine.connect() as connection:
        tables = await connection.run_sync(
            lambda conn: set(inspect(conn).get_table_names())
        )
    assert set(Base.metadata.tables).issubset(tables)


@pytest.mark.asyncio
async def test_parallel_claim_has_exactly_one_owner(mysql_sessions):
    sessions, _ = mysql_sessions
    project = "ci-" + uuid4().hex
    async with sessions() as db, db.begin():
        db.add(
            AnalysisJob(
                project_id=project,
                kind="test",
                subject_id="one",
                idempotency_hash=stable_hash(project),
            )
        )
    queues = [JobQueue(sessions, f"worker-{index}") for index in range(8)]
    leases = await asyncio.gather(*(queue.claim() for queue in queues))
    winners = [lease for lease in leases if lease and lease.project_id == project]
    assert len(winners) == 1
    winner = winners[0]
    owner = next(queue for queue in queues if queue.owner == winner.owner)

    async def complete(_):
        pass

    await owner.commit(winner, complete)
