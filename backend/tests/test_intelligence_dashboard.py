import pytest

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.dashboard import project_dashboard
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AnalysisJob
from app.main import app
from app.models.models import QueryStatus
from tests.test_intelligence_foundation import fixture_audit


@pytest.mark.asyncio
async def test_latest_audit_selection_never_reuses_old_success_or_another_project(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    old = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", old.id)
    run.status = "ready"
    new = await fixture_audit(db_session)
    new.status = QueryStatus.RUNNING
    await fixture_audit(db_session, project="p2")
    await db_session.commit()
    data = await project_dashboard(db_session, "p1")
    assert data["latest_audit"]["id"] == str(new.id)
    assert data["report"]["audit_id"] == str(new.id)
    assert data["report"]["status"] == "needs_analysis"
    assert data["processing"] is True and "entities" not in data["report"]
    assert "overall_score" not in data and "trend" not in data


@pytest.mark.asyncio
async def test_jobs_are_scoped_to_selected_audit_and_project(db_session, monkeypatch):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    old = await fixture_audit(db_session)
    current = await fixture_audit(db_session)
    for project, audit_id, key in (("p1", old.id, "a"), ("p2", current.id, "b")):
        db_session.add(
            AnalysisJob(
                project_id=project,
                kind="fetch_document",
                subject_id="e" * 32,
                payload={"audit_id": audit_id},
                idempotency_hash=key * 64,
            )
        )
    await db_session.flush()
    assert (await project_dashboard(db_session, "p1"))["processing"] is False
    job = AnalysisJob(
        project_id="p1",
        kind="fetch_document",
        subject_id="f" * 32,
        payload={"audit_id": current.id},
        idempotency_hash="c" * 64,
    )
    db_session.add(job)
    await db_session.flush()
    assert (await project_dashboard(db_session, "p1"))["processing"] is True
    job.status = "failed"
    await db_session.flush()
    assert (await project_dashboard(db_session, "p1"))["processing"] is False
    from datetime import timedelta

    from app.utils.timezone import utcnow

    job.status, job.available_at = "queued", utcnow() + timedelta(hours=1)
    await db_session.flush()
    data = await project_dashboard(db_session, "p1")
    assert data["processing"] is True and data["waiting"] is True
    job.available_at = utcnow() - timedelta(seconds=1)
    await db_session.flush()
    assert (await project_dashboard(db_session, "p1"))["waiting"] is False


@pytest.mark.asyncio
async def test_dashboard_scope_empty_and_disabled_state(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "role": "viewer",
    }
    try:
        assert (
            await client.get("/api/intelligence/projects/p2/dashboard")
        ).status_code == 403
        result = (await client.get("/api/intelligence/projects/p1/dashboard")).json()
        assert result["latest_audit"] is None and result["report"] is None
        await fixture_audit(db_session)
        monkeypatch.setattr(settings, "intelligence_enabled", False)
        result = (await client.get("/api/intelligence/projects/p1/dashboard")).json()
        assert result["enabled"] is False and result["latest_audit"] is None
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_collection_excludes_large_analysis_and_skips_metric_reconstruction(db_session, monkeypatch):
    import json

    from app.intelligence import reporting
    from app.intelligence.models import AuditIntelligenceState

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    audit = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", audit.id)
    run.status = "partial"
    run.coverage = {"sources": 100, "captured_sources": 50, "total_characters": 12345,
                    "analyzed_characters": 4567, "deferred_operations": ["x" * 100000]}
    target = {"target_id": "registry:own", "name": "Own", "role": "own", "identity_status": "confirmed"}
    run.summary = {"platforms": [{"platform": "summary_platform"}],
                   "brand_presence": {"targets": [{**target, "names": ["unused"]}], "observations": ["x" * 1000000]},
                   "diagnoses": ["x" * 1000000], "opportunities": {"items": ["x" * 1000000]}}
    state = AuditIntelligenceState(project_id="p1", audit_id=audit.id)
    db_session.add(state)
    await db_session.flush()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Collection must not reconstruct evidence/metric populations")

    monkeypatch.setattr(reporting, "run_snapshot", forbidden)
    monkeypatch.setattr(reporting, "review_snapshot", forbidden)
    data = await project_dashboard(db_session, "p1", collection=True)
    report = data["report"]
    assert report["run_id"] == run.id and report["revision"] == run.revision
    assert report["status"] == "partial" and report["snapshot_current"] is True
    assert report["coverage"]["total_characters"] == 12345
    assert report["coverage"]["captured_sources"] == 50
    assert report["analysis_scope"]["targets"] == [target]
    assert report["analysis_scope"]["audit_id"] == str(audit.id)
    assert report["analysis_scope"]["platforms"] == list(dict.fromkeys([*[row["platform"] for row in run.manifest["responses"]], "summary_platform"]))
    assert not {"diagnoses", "opportunities", "platforms", "entities", "source_observations"}.intersection(report)
    assert len(json.dumps(data).encode()) < 4000
    state.input_revision += 1
    await db_session.flush()
    stale = (await project_dashboard(db_session, "p1", collection=True))["report"]
    assert stale["status"] == "stale" and stale["snapshot_current"] is False


@pytest.mark.asyncio
async def test_collection_route_is_explicit_and_project_scoped(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    await fixture_audit(db_session)
    app.dependency_overrides[get_current_user] = lambda: {"scope": "project", "pid": "p1", "role": "viewer"}
    try:
        assert (await client.get("/api/intelligence/projects/p2/dashboard?view=collection")).status_code == 403
        assert (await client.get("/api/intelligence/projects/p1/dashboard?view=unknown")).status_code == 422
        data = (await client.get("/api/intelligence/projects/p1/dashboard?view=collection")).json()
        assert data["report"]["status"] == "needs_analysis"
        assert "analysis_scope" not in data["report"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
