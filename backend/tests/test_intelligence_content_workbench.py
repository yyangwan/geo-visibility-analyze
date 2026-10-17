from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.content_workbench import content_workbench
from app.intelligence.drafts import authorize_draft, export_brief, grant_signature
from app.intelligence.models import (
    ActionPlan,
    AuditIntelligenceState,
    DraftApproval,
    IntelligenceRun,
    PlatformDiagnosis,
)
from app.main import app
from app.utils.timezone import utcnow
from tests.test_intelligence_drafts import fixture_draft


@pytest.mark.asyncio
async def test_valid_material_projection_is_read_only_and_never_grants_publication(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    _, run, plan, _, request = await fixture_draft(db_session)
    grant = await authorize_draft(db_session, plan, run, request, "reviewer")
    await db_session.commit()
    data = await content_workbench(db_session, "p1")
    assert len(data["items"]) == 1 and data["items"][0]["status"] == "ready"
    assert data["items"][0]["run_id"] == run.id
    assert data["items"][0]["execution"] == {
        "channel": "wechat",
        "mode": "generate_draft",
    }
    assert not data["publication_allowed"] and data["items"][0]["fact_count"] == 1
    assert "facts" not in data["items"][0] and "signature" not in data["items"][0]
    assert not db_session.new and not db_session.dirty
    assert (await content_workbench(db_session, "p2"))["items"] == []
    with pytest.raises(HTTPException) as error:
        await content_workbench(db_session, "p2", approval_id=grant.id)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_newer_expired_version_never_falls_back_to_an_old_approval(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    _, run, plan, _, request = await fixture_draft(db_session)
    first = await authorize_draft(db_session, plan, run, request, "reviewer")
    newer = await authorize_draft(
        db_session,
        plan,
        run,
        request.model_copy(
            update={"expected_version": 1, "idempotency_key": "new-draft-approval"}
        ),
        "reviewer",
    )
    newer.expires_at = utcnow() - timedelta(seconds=1)
    newer.signature = grant_signature(newer)
    await db_session.commit()
    data = await content_workbench(db_session, "p1")
    assert [item["id"] for item in data["items"]] == [newer.id]
    assert data["items"][0]["status"] == "expired"
    assert (await content_workbench(db_session, "p1", approval_id=first.id))["items"][
        0
    ]["status"] == "stale"
    newer.payload = {**newer.payload, "title": "被篡改的材料标题"}
    data = await content_workbench(db_session, "p1")
    assert data["items"][0]["status"] == "invalid"
    assert data["items"][0]["title"] != newer.payload["title"]


@pytest.mark.asyncio
async def test_new_queued_run_and_missing_state_block_export_without_creating_state(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    _, run, plan, _, request = await fixture_draft(db_session)
    grant = await authorize_draft(db_session, plan, run, request, "reviewer")
    await db_session.commit()
    newer = IntelligenceRun(
        project_id="p1",
        audit_id=run.audit_id,
        revision=run.revision + 1,
        signature=run.signature,
        input_hash="n" * 64,
        manifest={},
        status="queued",
    )
    db_session.add(newer)
    await db_session.flush()
    assert (await content_workbench(db_session, "p1"))["items"][0]["status"] == "stale"
    with pytest.raises(HTTPException):
        await export_brief(db_session, grant, plan, run)
    await db_session.execute(
        delete(IntelligenceRun).where(IntelligenceRun.id == newer.id)
    )
    await db_session.execute(
        delete(AuditIntelligenceState).where(
            AuditIntelligenceState.project_id == "p1",
            AuditIntelligenceState.audit_id == run.audit_id,
        )
    )
    await db_session.commit()
    assert (await content_workbench(db_session, "p1"))["items"][0]["status"] == "stale"
    assert not db_session.new


@pytest.mark.asyncio
async def test_cursor_pages_do_not_skip_or_repeat_equal_timestamp_records(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    _, run, plan, _, request = await fixture_draft(db_session)
    await authorize_draft(db_session, plan, run, request, "reviewer")
    moment = utcnow()
    for index in range(31):
        diagnosis = PlatformDiagnosis(
            project_id="p1",
            run_id=run.id,
            diagnosis_hash=str(index).zfill(64),
            payload={},
        )
        db_session.add(diagnosis)
        await db_session.flush()
        action = ActionPlan(
            project_id="p1",
            run_id=run.id,
            diagnosis_id=diagnosis.id,
            title="分页行动",
            target={},
            actor_id="test",
            idempotency_key=f"plan-{index}",
        )
        db_session.add(action)
        await db_session.flush()
        db_session.add(
            DraftApproval(
                id=uuid4().hex,
                project_id="p1",
                action_id=action.id,
                version=1,
                payload={},
                input_stamp="s" * 64,
                signature="invalid",
                actor_id="test",
                reason="测试只读分页",
                expires_at=moment,
                idempotency_key=f"grant-{index}",
                request_hash="r" * 64,
                created_at=moment,
            )
        )
    await db_session.commit()
    first = await content_workbench(db_session, "p1")
    second = await content_workbench(db_session, "p1", cursor=first["next_cursor"])
    ids = [item["id"] for data in (first, second) for item in data["items"]]
    assert len(ids) == len(set(ids)) == 32 and second["next_cursor"] is None
    with pytest.raises(HTTPException):
        await content_workbench(db_session, "p2", cursor=first["next_cursor"])


@pytest.mark.asyncio
async def test_api_scope_disabled_and_invalid_selectors(client, monkeypatch):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "role": "viewer",
    }
    path = "/api/intelligence/projects/p1/content-workbench"
    try:
        assert (await client.get(path.replace("p1", "p2"))).status_code == 403
        assert (await client.get(path + "?cursor=wrong")).status_code == 422
        assert (
            await client.get(path + f"?cursor={'a' * 32}&approval_id={'b' * 32}")
        ).status_code == 422
        monkeypatch.setattr(settings, "intelligence_enabled", False)
        assert (await client.get(path)).json()["enabled"] is False
    finally:
        app.dependency_overrides.pop(get_current_user, None)
