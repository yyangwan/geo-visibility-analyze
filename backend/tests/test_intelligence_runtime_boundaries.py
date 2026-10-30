import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, inspect, select, text

import app.main as main_module
from app.intelligence import worker
from app.intelligence.models import SamplingAudit
from app.intelligence.queue import LeaseLost
from app.models.models import Audit, QueryStatus
from tests.conftest import TestSessionLocal


def lease(kind="analyze_run"):
    return SimpleNamespace(id="test-job", kind=kind)


@pytest.mark.asyncio
async def test_lost_heartbeat_cancels_inflight_work_without_retry(monkeypatch, caplog):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def work(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def lost(_):
        await started.wait()
        raise LeaseLost("secret-lease-details")

    proxy = SimpleNamespace(
        **{
            name: getattr(asyncio, name)
            for name in ("create_task", "wait", "FIRST_COMPLETED", "gather")
        },
        sleep=AsyncMock(),
    )
    monkeypatch.setattr(worker, "asyncio", proxy)
    queue = SimpleNamespace(heartbeat=AsyncMock(side_effect=lost), fail=AsyncMock())
    await asyncio.wait_for(
        worker.execute(queue, SimpleNamespace(analyze_run=work), lease()), timeout=2
    )
    assert cancelled.is_set()
    queue.fail.assert_not_awaited()
    assert "lease_lost job=test-job" in caplog.text
    assert "secret-lease-details" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_lost_during_failure", [False, True])
async def test_pipeline_failure_records_only_error_class(
    caplog, lease_lost_during_failure
):
    item = lease()
    queue = SimpleNamespace(heartbeat=AsyncMock(), fail=AsyncMock())
    if lease_lost_during_failure:
        queue.fail.side_effect = LeaseLost("private-lease")
    pipeline = SimpleNamespace(
        analyze_run=AsyncMock(side_effect=ValueError("private-source-body"))
    )
    await worker.execute(queue, pipeline, item)
    queue.fail.assert_awaited_once_with(item, "ValueError")
    assert "class=ValueError" in caplog.text
    assert (
        "private-source-body" not in caplog.text and "private-lease" not in caplog.text
    )


@pytest.mark.asyncio
async def test_execute_cancellation_waits_for_work_cleanup():
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def work(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    queue = SimpleNamespace(heartbeat=AsyncMock(), fail=AsyncMock())
    task = asyncio.create_task(
        worker.execute(queue, SimpleNamespace(analyze_run=work), lease())
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    queue.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_job_kind_fails_without_starting_pipeline():
    queue = SimpleNamespace(fail=AsyncMock())
    item = lease("not-a-job")
    await worker.execute(queue, object(), item)
    queue.fail.assert_awaited_once_with(item, "unsupported_job_kind", retry=False)


@pytest.mark.parametrize(
    "revision", [None, "old-schema", "intelligence_worker_20261016", "evaluation_reviews_20261022", "citation_value_20261024", "citation_execution_20261026"]
)
def test_schema_startup_is_readonly_and_always_disposes(
    tmp_path, monkeypatch, revision
):
    url = f"sqlite:///{tmp_path / 'revision.db'}"
    engine = create_engine(url)
    if revision is not None:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(64))")
            )
            connection.execute(
                text("INSERT INTO alembic_version VALUES (:revision)"),
                {"revision": revision},
            )
    tables = inspect(engine).get_table_names()
    dispose = Mock(wraps=engine.dispose)
    monkeypatch.setattr(engine, "dispose", dispose)
    monkeypatch.setattr("sqlalchemy.create_engine", lambda _: engine)
    monkeypatch.setattr(main_module.settings, "database_url", url)
    if revision == "citation_execution_20261026":
        main_module._check_schema_revision()
    else:
        with pytest.raises(RuntimeError, match="Schema mismatch"):
            main_module._check_schema_revision()
    dispose.assert_called_once_with()
    assert inspect(engine).get_table_names() == tables
    if revision is not None:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == revision
            )
    engine.dispose()


@pytest.mark.asyncio
async def test_restart_recovers_only_legacy_audits(db_session, monkeypatch):
    audits = {}
    for kind in ("legacy", "sampling"):
        for state in (QueryStatus.PENDING, QueryStatus.RUNNING):
            audit = Audit(project_id="p1", status=state, brands_json=[])
            db_session.add(audit)
            await db_session.flush()
            audits[kind, state] = audit.id
            if kind == "sampling":
                db_session.add(
                    SamplingAudit(
                        project_id="p1",
                        audit_id=audit.id,
                        idempotency_key=f"sampling-{state.value}",
                        request_hash="a" * 64,
                        expected_samples=1,
                    )
                )
    await db_session.commit()
    resumed = []

    async def run_audit(audit_id):
        resumed.append(audit_id)

    monkeypatch.setattr("app.database.async_session", TestSessionLocal)
    monkeypatch.setattr("app.services.audit_service.run_audit", run_audit)
    await main_module._recover_orphan_audits()
    await asyncio.sleep(0)
    async with TestSessionLocal() as session:
        rows = {row.id: row for row in (await session.scalars(select(Audit))).all()}
        legacy_running = rows[audits["legacy", QueryStatus.RUNNING]]
        assert legacy_running.status == QueryStatus.FAILED
        assert legacy_running.completed_at is not None
        assert resumed == [audits["legacy", QueryStatus.PENDING]]
        for state in (QueryStatus.PENDING, QueryStatus.RUNNING):
            sampling = rows[audits["sampling", state]]
            assert sampling.status == state
            assert sampling.completed_at is None and sampling.error_message is None
