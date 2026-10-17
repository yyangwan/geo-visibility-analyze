"""Explicit recovery keeps immutable inputs, usage budgets and lease fencing."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AnalysisJob, IntelligenceRun
from app.intelligence.queue import JobQueue, LeaseLost
from app.intelligence.refresh import perform_refresh
from app.main import app
from tests.conftest import TestSessionLocal
from tests.test_intelligence_foundation import fixture_audit


async def failed_analysis(db, *, sources=None):
    audit = await fixture_audit(db, sources=sources)
    run = await prepare_run(db, "p1", audit.id)
    await db.flush()
    job = await db.scalar(select(AnalysisJob).where(AnalysisJob.run_id == run.id))
    job.max_attempts = 1
    job.payload = {"model_requests": 19, "reserved_usage": {"tokens": 42}}
    await db.commit()
    queue = JobQueue(TestSessionLocal, "original-worker")
    lease = await queue.claim()
    assert lease.id == job.id
    await queue.fail(lease, "provider_outage")
    await db.refresh(run)
    await db.refresh(job)
    assert run.status == job.status == "failed"
    return audit, run, job, queue, lease


@pytest.mark.asyncio
async def test_manual_retry_retains_inputs_budget_and_fences_old_worker(db_session):
    audit, run, job, queue, old_lease = await failed_analysis(db_session)
    immutable = (run.id, run.revision, run.manifest, run.input_hash, run.signature)
    retried = await prepare_run(db_session, "p1", audit.id, retry_failed=True)
    await db_session.commit()
    assert (
        retried.id,
        retried.revision,
        retried.manifest,
        retried.input_hash,
        retried.signature,
    ) == immutable
    assert retried.status == "queued" and retried.finished_at is None
    assert job.attempt == 0 and job.error_code is None
    assert job.lease_owner is job.lease_token is job.lease_until is None
    assert job.lease_version == old_lease.version + 1
    assert job.payload["model_requests"] == 19
    assert job.payload["reserved_usage"] == {"tokens": 42}
    assert job.payload["manual_retry_history"][-1]["error_code"] == "provider_outage"

    async def finish(db):
        current = await db.get(IntelligenceRun, run.id)
        current.status = "ready"

    with pytest.raises(LeaseLost):
        await queue.commit(old_lease, finish)
    new_lease = await queue.claim()
    assert new_lease.version > old_lease.version and new_lease.token != old_lease.token
    with pytest.raises(LeaseLost):
        await queue.commit(old_lease, finish)
    await queue.commit(new_lease, finish)
    await db_session.refresh(run)
    await db_session.refresh(job)
    assert run.status == "ready" and job.status == "succeeded"
    same = await prepare_run(db_session, "p1", audit.id, retry_failed=True)
    await db_session.commit()
    assert same.id == run.id and job.attempt == 1
    assert await queue.claim() is None


@pytest.mark.asyncio
async def test_default_refresh_does_not_retry_terminal_analysis(db_session):
    audit, run, job, _, _ = await failed_analysis(db_session)
    same = await prepare_run(db_session, "p1", audit.id)
    await perform_refresh(db_session, "p1", audit.id, "automatic-refresh")
    await db_session.commit()
    assert same.id == run.id and same.status == "failed"
    assert job.status == "failed" and job.attempt == 1
    assert "manual_retry_history" not in job.payload


@pytest.mark.asyncio
async def test_manual_retry_is_idempotent_and_does_not_reset_source_jobs(db_session):
    audit, _run, job, _, _ = await failed_analysis(
        db_session,
        sources=[
            {
                "url": "https://example.com/article",
                "kind": "answer_reference",
                "url_resolution": "exact",
            }
        ],
    )
    source = await db_session.scalar(
        select(AnalysisJob).where(AnalysisJob.kind == "fetch_document")
    )
    source.status, source.attempt, source.error_code = "failed", 3, "source_outage"
    await db_session.commit()
    await prepare_run(db_session, "p1", audit.id, retry_failed=True)
    await db_session.commit()
    version = job.lease_version
    await prepare_run(db_session, "p1", audit.id, retry_failed=True)
    await db_session.commit()
    assert job.lease_version == version
    assert len(job.payload["manual_retry_history"]) == 1
    assert (source.status, source.attempt, source.error_code) == (
        "failed",
        3,
        "source_outage",
    )


@pytest.mark.asyncio
async def test_inconsistent_failed_analysis_returns_conflict(db_session):
    audit, _, job, _, _ = await failed_analysis(db_session)
    job.status = "succeeded"
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await prepare_run(db_session, "p1", audit.id, retry_failed=True)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_manual_retry_cannot_revive_a_superseded_failed_run(db_session):
    audit, run, job, _, _ = await failed_analysis(db_session)
    db_session.add(
        IntelligenceRun(
            project_id="p1",
            audit_id=audit.id,
            revision=run.revision + 1,
            input_hash="f" * 64,
            signature=run.signature,
            manifest=run.manifest,
        )
    )
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await prepare_run(db_session, "p1", audit.id, retry_failed=True)
    assert error.value.status_code == 409
    assert job.status == "failed"


@pytest.mark.asyncio
async def test_manual_retry_endpoint_checks_project_and_writer(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    audit, run, job, _, _ = await failed_analysis(db_session)
    try:
        for project, role, expected in [("p2", "owner", 403), ("p1", "viewer", 403)]:
            app.dependency_overrides[get_current_user] = lambda p=project, r=role: {
                "scope": "project",
                "pid": p,
                "role": r,
            }
            response = await client.post(f"/api/intelligence/audits/{audit.id}/runs")
            assert response.status_code == expected
            assert job.status == "failed"
        app.dependency_overrides[get_current_user] = lambda: {
            "scope": "project",
            "pid": "p1",
            "role": "owner",
        }
        response = await client.post(f"/api/intelligence/audits/{audit.id}/runs")
        assert response.status_code == 202
        assert response.json()["id"] == run.id
        assert response.json()["status"] == "queued"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
