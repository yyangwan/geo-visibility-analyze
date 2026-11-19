"""Verify PostgreSQL locks, lease timing, and visibility audit concurrency."""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import create_engine, func, select

from app.config import settings
from app.database import async_session, engine, sync_database_url
from app.intelligence.blob import LocalBlobStore, digest
from app.intelligence.capture_contract import SamplingRequest
from app.intelligence.models import AnalysisJob, CapturedResponse, IntelligenceRun
from app.intelligence.pipeline import Pipeline
from app.intelligence.queue import JobQueue, db_now
from app.intelligence.sampling import create_sampling_audit
from app.intelligence.schema_lock import SchemaLockUnavailable, schema_lock
from app.intelligence.sources import CapturedSource
from app.intelligence.visibility_flow import retry_analysis, settle_flow
from app.models.device_gateway import DeviceTask
from app.models.models import Prompt
from app.services.device_gateway_service import claim_task, complete_task, touch_gateway


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
    async with async_session() as db, db.begin():
        earlier = await db_now(db)
        await asyncio.sleep(0.05)
        assert await db_now(db) > earlier


def sample_result(task):
    text = "已核对的测试回答。"
    return {
        "contractVersion": 2,
        "taskId": task.id,
        "platform": task.platform,
        "surface": task.surface,
        "observedAt": datetime.now(UTC).isoformat(),
        "answer": {
            "text": text,
            "status": "complete",
            "contentHash": digest(text.encode()),
        },
        "conditions": {"searchMode": "unknown"},
        "sources": [
            {
                "ordinal": 1,
                "urlResolution": "exact",
                "kind": "answer_reference",
                "title": "测试归档",
                "url": "https://example.com/isolation-fixture",
            }
        ],
        "sourcePanel": {"status": "complete", "expectedCount": 1, "capturedCount": 1},
        "adapter": {"name": "postgresql-runtime-check", "version": "2"},
    }


async def check_visibility_concurrency():
    """Run the real locks against the explicitly configured local QA database."""
    project = "postgres-v4-" + uuid4().hex
    gateway = settings.mobile_app_capture_gateway_id or "postgresql-runtime-check"
    async with async_session() as db:
        await touch_gateway(db, gateway)
        prompt = Prompt(project_id=project, text="如何验证测试数据归档？")
        db.add(prompt)
        await db.commit()
        prompt_id = prompt.id

    request = SamplingRequest.model_validate(
        {
            "promptIds": [prompt_id],
            "platforms": ["qwen", "deepseek"],
            "rounds": 1,
            "surface": "managed_v1",
            "processingMode": "v4",
            "idempotencyKey": uuid4().hex,
        }
    )

    async def start():
        async with async_session() as db, db.begin():
            audit, count = await create_sampling_audit(db, project, request, [])
            assert count == 2
            return audit.id

    audits = await asyncio.gather(start(), start())
    audit_id = audits[0]
    assert audits[1] == audit_id, "Duplicate start created a second audit"

    async def claim(capability):
        async with async_session() as db:
            task, token = await claim_task(db, gateway, [capability])
            assert task and task.project_id == project and token
            return task.id, token, sample_result(task)

    claims = await asyncio.gather(claim("browser.prompt"), claim("appium.prompt"))

    async def accept(task_id, token, payload):
        async with async_session() as db:
            assert (
                await complete_task(db, gateway, task_id, token, payload)
            ).status == "completed"

    await asyncio.gather(*(accept(*claim) for claim in [*claims, claims[0]]))

    async def settle():
        async with async_session() as db, db.begin():
            return await settle_flow(db, project, audit_id)

    assert not await settle(), "Source archive was still pending"

    async def archive(url, _limit):
        return CapturedSource(
            url, 200, b"fixture", "Only local archive verification.", "complete"
        )

    queue = JobQueue(async_session, "postgresql-runtime-check")
    with TemporaryDirectory(prefix="visibility-v4-test-") as temporary:
        pipeline = Pipeline(queue, LocalBlobStore(Path(temporary)), capture=archive)
        source = await queue.claim()
        assert (
            source and source.kind == "fetch_document" and source.project_id == project
        )
        await pipeline.fetch_document(source)
        assert sum(await asyncio.gather(settle(), settle(), settle())) == 1
        lease = await queue.claim()
        assert lease and lease.project_id == project and lease.kind == "analyze_run"
        assert lease.payload["visibility_attempt"] == 1
        await queue.fail(lease, "local_fixture_failure", retry=True)

        async def retry():
            async with async_session() as db, db.begin():
                job = await retry_analysis(
                    db, project, audit_id, lease.id, "test-agent"
                )
                return job.id

        retries = await asyncio.gather(retry(), retry(), retry())
        assert len(set(retries)) == 1, "Concurrent retry created duplicate attempts"

        async with async_session() as db:
            runs = (
                await db.scalars(
                    select(IntelligenceRun).where(
                        IntelligenceRun.project_id == project,
                        IntelligenceRun.audit_id == audit_id,
                    )
                )
            ).all()
            assert len(runs) == 1
            assert len(runs[0].manifest["responses"]) == 2
            assert len(runs[0].manifest["sources"]) == 2
            assert len({row["snapshot_id"] for row in runs[0].manifest["sources"]}) == 1
            assert all(row["snapshot_id"] for row in runs[0].manifest["sources"])
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(DeviceTask)
                    .where(
                        DeviceTask.project_id == project,
                    )
                )
            ) == 2
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(CapturedResponse)
                    .where(
                        CapturedResponse.project_id == project,
                    )
                )
            ) == 2
            matching = (
                await db.scalars(
                    select(AnalysisJob).where(
                        AnalysisJob.project_id == project,
                        AnalysisJob.kind == "analyze_run",
                    )
                )
            ).all()
            assert sorted(job.payload["visibility_attempt"] for job in matching) == [
                1,
                2,
            ]
            assert {job.run_id for job in matching} == {runs[0].id}

        second = await queue.claim()
        assert second and second.id == retries[0]
        await queue.fail(second, "local_fixture_finished", retry=False)


async def check_runtime():
    try:
        await check_clock()
        await check_visibility_concurrency()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    url = sync_database_url()
    if (
        url.drivername != "postgresql+psycopg"
        or url.database not in {"analysis_pg_ci", "analysis_pg_local"}
        or url.host not in {"127.0.0.1", "localhost"}
    ):
        raise SystemExit("A dedicated local PostgreSQL test database is required")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    check_lock()
    asyncio.run(check_runtime())
    print(
        "PostgreSQL advisory lock, lease timing and v4 capture/settlement/retry concurrency verified"
    )
