from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.measurement import CompareRequest, compare_cohort
from app.intelligence.measurement_history import measurement_history
from app.intelligence.models import (
    AuditIntelligenceState,
    IntelligenceRun,
    MeasurementCohort,
)
from app.intelligence.semantics import stable_hash
from app.main import app
from app.utils.timezone import utcnow
from tests.test_intelligence_foundation import fixture_audit
from tests.test_intelligence_measurement import fixture_cohort


async def saved_result(db):
    cohort, baseline, _, _ = await fixture_cohort(db)
    audit = await fixture_audit(db)
    followup = IntelligenceRun(
        project_id="p1",
        audit_id=audit.id,
        revision=1,
        input_hash="f" * 64,
        signature=baseline.signature,
        manifest={},
        summary={},
        status="partial",
    )
    db.add(followup)
    await db.flush()
    row = await compare_cohort(
        db, cohort, CompareRequest(followupRunId=followup.id), "tester"
    )
    for run in (baseline, followup):
        db.add(
            AuditIntelligenceState(
                project_id="p1", audit_id=run.audit_id, published_run_id=run.id
            )
        )
    await db.commit()
    return cohort, baseline, followup, row


@pytest.mark.asyncio
async def test_saved_results_retain_exact_inputs_and_new_revision_invalidates_currentness(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    monkeypatch.setattr(
        "app.intelligence.measurement_history.stamp",
        AsyncMock(return_value=("stamp", True)),
    )
    cohort, baseline, followup, row = await saved_result(db_session)
    data = await measurement_history(db_session, "p1")
    assert data["results"][0]["id"] == row.id
    assert data["results"][0]["snapshot_current"]
    assert data["results"][0]["baseline_run_id"] == baseline.id
    assert data["results"][0]["followup_run_id"] == followup.id
    db_session.add(
        IntelligenceRun(
            project_id="p1",
            audit_id=followup.audit_id,
            revision=2,
            signature=followup.signature,
            input_hash="n" * 64,
            manifest={},
            status="queued",
        )
    )
    await db_session.flush()
    stale = await measurement_history(db_session, "p1", cohort.id)
    assert not stale["results"][0]["snapshot_current"]
    assert stale["results"][0]["followup_run_id"] == followup.id
    row.result = {**row.result, "followup_input_hash": "tampered"}
    with pytest.raises(HTTPException) as error:
        await measurement_history(db_session, "p1", cohort.id)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_latest_empty_cohort_does_not_fall_back_and_history_cutoff_applies(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    old, baseline, _, _ = await saved_result(db_session)
    newer = MeasurementCohort(
        project_id="p1",
        baseline_run_id=baseline.id,
        title="新的空复测方案",
        specification=old.specification,
        specification_hash=stable_hash(old.specification),
        actor_id="tester",
        idempotency_key="new-cohort",
        request_hash="r" * 64,
        created_at=utcnow() + timedelta(seconds=1),
    )
    db_session.add(newer)
    await db_session.flush()
    data = await measurement_history(db_session, "p1")
    assert data["selected_cohort_id"] == newer.id and data["results"] == []
    assert (await measurement_history(db_session, "p1", old.id))["results"]
    cutoff = utcnow() + timedelta(days=1)
    assert (await measurement_history(db_session, "p1", since=cutoff))["cohorts"] == []
    with pytest.raises(HTTPException):
        await measurement_history(db_session, "p1", old.id, cutoff)
    with pytest.raises(HTTPException):
        await measurement_history(db_session, "p2", old.id)


@pytest.mark.asyncio
async def test_history_api_scope_validation_and_disabled_state(
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
            await client.get("/api/intelligence/projects/p2/measurement-history")
        ).status_code == 403
        assert (
            await client.get(
                "/api/intelligence/projects/p1/measurement-history?history_days=0"
            )
        ).status_code == 422
        data = (
            await client.get("/api/intelligence/projects/p1/measurement-history")
        ).json()
        assert data["results"] == [] and data["selected_cohort_id"] is None
        await fixture_cohort(db_session)
        monkeypatch.setattr(settings, "intelligence_enabled", False)
        data = (
            await client.get("/api/intelligence/projects/p1/measurement-history")
        ).json()
        assert data["enabled"] is False and data["cohorts"] == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)
