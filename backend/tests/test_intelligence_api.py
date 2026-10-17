import pytest
from sqlalchemy import select

from app.api.auth import get_current_user
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AnalysisJob
from app.main import app
from tests.test_intelligence_foundation import fixture_audit


@pytest.mark.asyncio
async def test_capture_progress_counts_source_and_analysis_jobs(
    client, db_session, monkeypatch
):
    from app.config import settings
    from app.intelligence.models import IntelligenceRun
    from app.models.models import Prompt

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    await fixture_audit(db_session)
    prompt_id = await db_session.scalar(select(Prompt.id))
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "role": "owner",
    }
    try:
        started = await client.post(
            "/api/intelligence/projects/p1/audits",
            json={
                "sampling": {
                    "promptIds": [prompt_id],
                    "platforms": ["qwen"],
                    "rounds": 1,
                    "surface": "web",
                    "idempotencyKey": "progress-key-123",
                },
                "brands": [],
            },
        )
        assert started.status_code == 202
        audit_id = int(started.json()["audit_id"])
        run = IntelligenceRun(
            project_id="p1",
            audit_id=audit_id,
            revision=1,
            manifest={},
            input_hash="a" * 64,
            signature="b" * 64,
        )
        db_session.add(run)
        await db_session.flush()
        db_session.add_all(
            [
                AnalysisJob(
                    project_id="p1",
                    kind="analyze_run",
                    run_id=run.id,
                    subject_id=run.id,
                    idempotency_hash="c" * 64,
                ),
                AnalysisJob(
                    project_id="p1",
                    kind="fetch_document",
                    subject_id="d" * 32,
                    payload={"audit_id": audit_id},
                    idempotency_hash="d" * 64,
                ),
                AnalysisJob(
                    project_id="p2",
                    kind="fetch_document",
                    subject_id="e" * 32,
                    payload={"audit_id": audit_id},
                    idempotency_hash="e" * 64,
                ),
            ]
        )
        await db_session.commit()
        response = await client.get(f"/api/intelligence/audits/{audit_id}/capture")
        assert response.status_code == 200
        assert (
            response.json()["source_pending"]
            == response.json()["analysis_pending"]
            == 1
        )
        assert response.json()["expected_samples"] == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_source_retry_is_scoped_and_keeps_idempotency_after_active_job_finishes(
    client, db_session, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    audit = await fixture_audit(
        db_session,
        sources=[
            {
                "url": "https://example.com/article",
                "kind": "answer_reference",
                "urlResolution": "exact",
                "title": "article",
            }
        ],
    )
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    document_id = next(
        item["document_id"] for item in run.manifest["sources"] if item["document_id"]
    )

    async def user():
        return {"scope": "project", "pid": "p1", "role": "owner"}

    app.dependency_overrides[get_current_user] = user
    try:
        body = {"auditId": audit.id, "idempotencyKey": "source-retry-key"}
        first = await client.post(
            f"/api/intelligence/sources/{document_id}/retry", json=body
        )
        assert first.status_code == 202
        job = await db_session.scalar(
            select(AnalysisJob).where(AnalysisJob.id == first.json()["job_id"])
        )
        job.status = "succeeded"
        await db_session.commit()
        second = await client.post(
            f"/api/intelligence/sources/{document_id}/retry", json=body
        )
        assert second.json()["job_id"] == first.json()["job_id"]
        assert (
            await client.post(f"/api/intelligence/sources/{'f' * 32}/retry", json=body)
        ).status_code == 404
        app.dependency_overrides[get_current_user] = lambda: {
            "scope": "project",
            "pid": "p2",
            "role": "owner",
        }
        assert (
            await client.post(
                f"/api/intelligence/sources/{document_id}/retry", json=body
            )
        ).status_code == 403
        app.dependency_overrides[get_current_user] = lambda: {
            "scope": "project",
            "pid": "p1",
            "role": "viewer",
        }
        assert (
            await client.post(
                f"/api/intelligence/sources/{document_id}/retry", json=body
            )
        ).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_other_project_cannot_read_run_or_content(client, db_session):
    audit = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()

    async def user():
        return {"scope": "project", "pid": "p2", "role": "viewer"}

    app.dependency_overrides[get_current_user] = user
    try:
        assert (await client.get(f"/api/intelligence/runs/{run.id}")).status_code == 404
        assert (
            await client.get(
                f"/api/intelligence/runs/{run.id}/content?owner_kind=answer&owner_id=1"
            )
        ).status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_viewer_cannot_start_analysis(client, db_session, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    audit = await fixture_audit(db_session)

    async def user():
        return {"scope": "project", "pid": "p1", "role": "viewer"}

    app.dependency_overrides[get_current_user] = user
    try:
        assert (
            await client.post(f"/api/intelligence/audits/{audit.id}/runs")
        ).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_owner_creation_is_durable_and_idempotent(
    client, db_session, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    audit = await fixture_audit(db_session)

    async def user():
        return {"scope": "project", "pid": "p1", "role": "owner"}

    app.dependency_overrides[get_current_user] = user
    try:
        first = await client.post(f"/api/intelligence/audits/{audit.id}/runs")
        second = await client.post(f"/api/intelligence/audits/{audit.id}/runs")
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["status"] == "queued"
        assert "text" not in first.json()["responses"][0]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
