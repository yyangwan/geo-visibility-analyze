from datetime import UTC, timedelta

import pytest
from fastapi import HTTPException

from app.intelligence.actions import (
    ApprovalRequest,
    PlanRequest,
    approve_plan,
    create_plan,
    persist_diagnoses,
)
from app.intelligence.drafts import DraftRequest, authorize_draft, export_brief
from app.intelligence.ingestion import prepare_run
from app.intelligence.refresh import state_for_audit
from app.intelligence.reviews import (
    WithdrawRequest,
    approve_fact,
    review_claim,
    withdraw_fact,
)
from app.utils.timezone import utcnow
from tests.test_intelligence_reviews import (
    fact_request,
    fixture_evidence,
    review_request,
)


async def fixture_draft(db):
    origin, evidence, entity, source = await fixture_evidence(db)
    review = await review_claim(
        db, origin, evidence.id, review_request(source.text), "reviewer"
    )
    fact = await approve_fact(db, origin, fact_request(review, entity), "reviewer")
    await db.commit()
    run = await prepare_run(db, "p1", origin.audit_id, enrich=False)
    run.status = "partial"
    state = await state_for_audit(db, "p1", run.audit_id)
    state.handled_revision = state.input_revision
    summary = {
        "diagnoses": [
            {
                "id": "f" * 64,
                "kind": "source_concentration",
                "platform": "qwen",
                "prompt_hash": run.manifest["responses"][0]["prompt_hash"],
                "prompt": "有哪些方案",
                "source_ids": [run.manifest["sources"][0]["id"]],
                "action": {"task": "以事实说明产品质保范围"},
            }
        ]
    }
    await persist_diagnoses(db, run, summary)
    plan = await create_plan(
        db,
        run,
        PlanRequest(
            diagnosisId=summary["diagnoses"][0]["record_id"],
            idempotencyKey="plan-key-123",
        ),
        "reviewer",
    )
    await approve_plan(
        db,
        plan,
        run,
        ApprovalRequest(
            expectedRevision=1,
            reason="核实材料后开展内容实验",
            scope="investigation_plan",
            idempotencyKey="plan-approval-key",
        ),
        "reviewer",
        "owner",
    )
    await db.commit()
    request = DraftRequest(
        expectedVersion=0,
        factIds=[fact.id],
        channel="wechat",
        title="X系列产品质保范围说明",
        reason="已检查事实适用范围和平台证据",
        expiresAt=utcnow().replace(tzinfo=UTC) + timedelta(hours=1),
        idempotencyKey="draft-approval-key",
    )
    return origin, run, plan, fact, request


@pytest.mark.asyncio
async def test_draft_approval_replay_and_manual_website_dispatch(db_session):
    _, run, plan, fact, request = await fixture_draft(db_session)
    request = request.model_copy(update={"channel": "website"})
    grant = await authorize_draft(db_session, plan, run, request, "reviewer")
    await db_session.commit()
    assert (
        await authorize_draft(db_session, plan, run, request, "reviewer")
    ).id == grant.id
    brief = await export_brief(db_session, grant, plan, run)
    assert (
        brief["schemaVersion"] == 2 and brief["facts"][0]["statement"] == fact.statement
    )
    assert brief["execution"] == {"channel": "website", "mode": "manual_task"}
    assert (
        brief["aiTargets"][0]["platform"] == "qwen" and not brief["publicationAllowed"]
    )


@pytest.mark.asyncio
async def test_fact_withdrawal_and_payload_tamper_prevent_export(db_session):
    origin, run, plan, fact, request = await fixture_draft(db_session)
    grant = await authorize_draft(db_session, plan, run, request, "reviewer")
    await db_session.commit()
    original = grant.payload
    grant.payload = {**grant.payload, "title": "被改写的草稿标题"}
    with pytest.raises(HTTPException) as tamper:
        await export_brief(db_session, grant, plan, run)
    assert tamper.value.status_code == 409
    grant.payload = original
    await withdraw_fact(
        db_session,
        origin,
        fact,
        WithdrawRequest(
            expectedVersion=1,
            reason="事实依据更新需要撤回",
            idempotencyKey="withdraw-key-123",
        ),
        "reviewer",
    )
    with pytest.raises(HTTPException) as stale:
        await export_brief(db_session, grant, plan, run)
    assert stale.value.status_code == 409


@pytest.mark.asyncio
async def test_cross_platform_and_identity_task_reject_content_authorization(
    db_session,
):
    _, run, plan, _, request = await fixture_draft(db_session)
    plan.target = {**plan.target, "kind": "identity_review"}
    with pytest.raises(HTTPException) as identity:
        await authorize_draft(db_session, plan, run, request, "reviewer")
    assert identity.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,project,expected",
    [
        ("owner", "p1", 202),
        ("member", "p1", 403),
        ("viewer", "p1", 403),
        ("owner", "p2", 404),
    ],
)
async def test_draft_api_editor_scope(
    client, db_session, monkeypatch, role, project, expected
):
    from app.api.auth import get_current_user
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    _, _, plan, _, request = await fixture_draft(db_session)
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": project,
        "role": role,
        "sub": "reviewer",
    }
    try:
        result = await client.post(
            f"/api/intelligence/actions/{plan.id}/draft-approvals",
            json=request.model_dump(mode="json", by_alias=True),
        )
        assert result.status_code == expected
        if expected == 202:
            approval_id = result.json()["approval_id"]
            assert (await client.get(f"/api/intelligence/draft-approvals/{approval_id}/brief")).status_code == 200
            monkeypatch.setattr(settings, "intelligence_enabled", False)
            assert (await client.get(f"/api/intelligence/draft-approvals/{approval_id}/brief")).status_code == 503
    finally:
        app.dependency_overrides.pop(get_current_user, None)
