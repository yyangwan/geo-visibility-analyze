import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.api.auth import get_current_user
from app.intelligence.models import (
    AnalysisJob,
    SourceDocument,
    SourceFetch,
    SourceSnapshot,
    WorkerHeartbeat,
)
from app.intelligence.monitor import WorkerMonitor
from app.intelligence.operations import operations_view
from app.intelligence.queue import db_now
from app.main import app
from tests.conftest import TestSessionLocal


@pytest.mark.asyncio
async def test_idle_working_stop_and_database_clock(db_session, monkeypatch):
    from app.intelligence import monitor as module

    monitor = WorkerMonitor(TestSessionLocal, object())
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    await monitor.probe()
    row = await db_session.get(WorkerHeartbeat, monitor.id)
    assert row.state == "idle"
    assert row.last_seen_at == await db_now(db_session)
    assert row.storage_status == "development_local"
    assert row.disk_status == "low"
    assert row.storage_checked_at is not None
    monitor.state = "working"
    await monitor.beat()
    await db_session.refresh(row)
    assert row.state == "working"
    await monitor.close()
    await db_session.refresh(row)
    assert row.state == "stopped"
    assert (await operations_view(db_session, "p1"))["worker"]["status"] == "missing"


@pytest.mark.asyncio
async def test_probe_failures_do_not_expose_provider_details(
    db_session, monkeypatch, caplog
):
    from app.intelligence import monitor as module

    class Broken:
        async def verify_configuration(self):
            raise ValueError("secret-bucket access-key https://private.example")

    def broken_disk(_):
        raise OSError("sensitive-directory")

    monkeypatch.setattr(module.shutil, "disk_usage", broken_disk)
    monitor = WorkerMonitor(TestSessionLocal, Broken())
    await monitor.probe()
    row = await db_session.get(WorkerHeartbeat, monitor.id)
    assert row.storage_status == "failed" and row.disk_status == "unknown"
    assert "ValueError" in caplog.text and "OSError" in caplog.text
    assert (
        "secret-bucket" not in caplog.text and "sensitive-directory" not in caplog.text
    )


@pytest.mark.asyncio
async def test_heartbeat_loop_runs_without_claimed_jobs_and_survives_db_error(
    monkeypatch, caplog
):
    from app.intelligence import monitor as module

    monitor = WorkerMonitor(TestSessionLocal, object())
    called = []

    async def beat():
        called.append(monitor.state)
        if len(called) == 1:
            raise RuntimeError("secret connection string")

    async def sleep(_):
        if len(called) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(monitor, "beat", beat)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await monitor.heartbeat_loop()
    assert called == ["idle", "idle"]
    assert (
        "worker_heartbeat_error" in caplog.text
        and "secret connection" not in caplog.text
    )


@pytest.mark.asyncio
async def test_empty_diagnostics_are_unknown_and_read_only(db_session):
    view = await operations_view(db_session, "p1")
    assert view["worker"]["status"] == "unknown"
    assert view["worker"]["storage_configuration"] == {}
    assert view["sources"]["snapshot_integrity_records"] == {}
    assert view["sources"]["live_integrity_check"] == "not_performed"
    assert view["usage"]["billed_cost"] is None
    assert view["usage"]["oss_traffic_bytes"] is None
    assert view["backup_restore_status"] == "not_connected"
    assert not db_session.new and not db_session.dirty


@pytest.mark.asyncio
async def test_recent_workers_ignore_stopped_stale_future_and_expired_probes(
    db_session,
):
    now = await db_now(db_session)
    for number, state, seen, checked, storage in [
        (
            1,
            "idle",
            now - timedelta(seconds=20),
            now - timedelta(seconds=20),
            "configuration_verified",
        ),
        (2, "working", now, now - timedelta(seconds=100), "configuration_verified"),
        (3, "stopped", now, now, "failed"),
        (4, "idle", now - timedelta(seconds=100), now, "failed"),
        (5, "idle", now + timedelta(seconds=300), now, "failed"),
    ]:
        db_session.add(
            WorkerHeartbeat(
                id=str(number) * 32,
                state=state,
                started_at=now,
                last_seen_at=seen,
                storage_status=storage,
                storage_checked_at=checked,
                disk_status="ok",
                disk_checked_at=checked,
            )
        )
    await db_session.commit()
    view = await operations_view(db_session, "p1")
    assert view["worker"]["status"] == "recent"
    assert view["worker"]["recent_count"] == 2
    assert view["worker"]["storage_configuration"] == {
        "configuration_verified": 1,
        "unknown": 1,
    }
    assert "111111" not in json.dumps(view)


@pytest.mark.asyncio
async def test_project_counts_exclude_future_retries_and_never_return_payloads(
    db_session,
):
    now = await db_now(db_session)
    for number, project, status, available, lease, requests in [
        (1, "p1", "queued", now - timedelta(seconds=30), None, 3),
        (2, "p1", "retry_wait", now + timedelta(seconds=300), None, 2),
        (3, "p1", "leased", now, now - timedelta(seconds=1), 4),
        (4, "p1", "failed", now, None, "corrupt"),
        (5, "p2", "queued", now, None, 1000),
    ]:
        db_session.add(
            AnalysisJob(
                id=str(number) * 32,
                project_id=project,
                kind="analyze_run",
                subject_id="a" * 32,
                status=status,
                available_at=available,
                lease_until=lease,
                idempotency_hash=str(number) * 64,
                payload={"model_requests": requests, "credential": "must-never-leak"},
                error_code="https://secret-bucket.example",
            )
        )
    await db_session.commit()
    view = await operations_view(db_session, "p1")
    assert view["queue"]["due"] == 1 and view["queue"]["expired_leases"] == 1
    assert view["queue"]["counts"] == {
        "queued": 1,
        "retry_wait": 1,
        "leased": 1,
        "failed": 1,
    }
    assert view["queue"]["recent_failures"] == [{"id": "4" * 32, "category": "other"}]
    assert view["usage"]["model_request_reservations"] == 9
    assert view["usage"]["unknown_job_records"] == 1
    assert "must-never-leak" not in json.dumps(
        view
    ) and "secret-bucket" not in json.dumps(view)
    assert (await db_session.get(AnalysisJob, "3" * 32)).status == "leased"


@pytest.mark.asyncio
async def test_sources_are_project_scoped_and_recorded_not_live(db_session):
    now = await db_now(db_session)
    for number, project in [(1, "p1"), (2, "p2")]:
        doc = SourceDocument(
            id=str(number) * 32,
            project_id=project,
            url="https://private.example",
            url_hash=str(number) * 64,
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(
            SourceSnapshot(
                project_id=project,
                document_id=doc.id,
                body_hash="b" * 64,
                representation_hash="r" * 64,
                text_key="secret-path",
                text_sha256="c" * 64,
                extractor_version="v1",
                integrity_status="verified",
            )
        )
        db_session.add_all(
            [
                SourceFetch(
                    project_id=project,
                    document_id=doc.id,
                    status="blocked",
                    created_at=now,
                ),
                SourceFetch(
                    project_id=project,
                    document_id=doc.id,
                    status="secret-status",
                    created_at=now - timedelta(days=2),
                ),
            ]
        )
    await db_session.commit()
    view = await operations_view(db_session, "p1")
    assert view["sources"]["fetches_last_24h"] == {"blocked": 1}
    assert view["sources"]["snapshot_integrity_records"] == {"verified": 1}
    assert "secret" not in json.dumps(view) and "private.example" not in json.dumps(
        view
    )


@pytest.mark.asyncio
async def test_operations_reader_requires_matching_project_scope(client):
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "role": "viewer",
    }
    assert (
        await client.get("/api/intelligence/projects/p2/operations")
    ).status_code == 403
    assert (
        await client.get("/api/intelligence/projects/p1/operations")
    ).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", [False, True])
async def test_worker_main_records_stop_on_idle_exit_or_queue_failure(
    db_session, monkeypatch, crash
):
    import app.main as main_module
    from app.intelligence import sampling, worker

    callbacks = []
    monitors = []

    class Monitor:
        def __init__(self, *_):
            self.state = "idle"
            self.closed = False
            monitors.append(self)

        async def beat(self):
            pass

        async def heartbeat_loop(self):
            await asyncio.Event().wait()

        async def probe_loop(self):
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    # Cancellation of concurrent sessions invalidates SQLite's single in-memory
    # connection. Test main's task lifecycle here; persistence is tested above.
    monkeypatch.setattr(worker, "WorkerMonitor", Monitor)
    monkeypatch.setattr(worker.settings, "intelligence_enabled", True)
    monkeypatch.setattr(worker, "async_session", TestSessionLocal)
    monkeypatch.setattr(main_module, "_check_schema_revision", lambda: None)
    monkeypatch.setattr(worker, "configured_store", object)
    monkeypatch.setattr(worker, "Pipeline", lambda *_: None)
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "add_signal_handler",
        lambda _, callback: callbacks.append(callback),
    )

    async def reconcile(_):
        pass

    class Queue:
        def __init__(self, *_):
            pass

        async def reap(self):
            pass

        async def claim(self):
            if crash:
                raise RuntimeError("queue unavailable")
            callbacks[0]()

    monkeypatch.setattr(sampling, "reconcile_capture_failures", reconcile)
    monkeypatch.setattr(worker, "JobQueue", Queue)
    if crash:
        with pytest.raises(RuntimeError, match="queue unavailable"):
            await worker.main()
    else:
        await worker.main()
    assert len(monitors) == 1 and monitors[0].closed


@pytest.mark.asyncio
async def test_worker_startup_storage_failure_is_sanitized(monkeypatch, caplog):
    import app.main as main_module
    from app.intelligence import worker

    class Broken:
        async def verify_configuration(self):
            raise ValueError("secret-bucket credentials")

    monkeypatch.setattr(worker.settings, "intelligence_enabled", True)
    monkeypatch.setattr(main_module, "_check_schema_revision", lambda: None)
    monkeypatch.setattr(worker, "configured_store", Broken)
    with pytest.raises(
        RuntimeError, match="Storage configuration verification failed"
    ) as error:
        await worker.main()
    assert error.value.__suppress_context__
    assert "secret-bucket" not in caplog.text
