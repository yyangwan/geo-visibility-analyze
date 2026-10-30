import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.intelligence.actions import (
    ApprovalRequest,
    PlanRequest,
    approve_plan,
    create_plan,
    persist_diagnoses,
    plan_view,
)
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import ActionApproval, IntelligenceRun
from app.intelligence.refresh import request_refresh
from tests.test_intelligence_foundation import fixture_audit


async def fixture_plan(db):
    audit = await fixture_audit(db)
    run = await prepare_run(db, "p1", audit.id)
    run.status = "partial"
    summary = {
        "diagnoses": [
            {
                "id": "a" * 64,
                "platform": "qwen",
                "prompt": "有哪些方案",
                "response_ids": ["1"],
                "source_ids": [],
                "evidence_ids": [],
                "action": {"task": "核实品牌身份"},
            }
        ]
    }
    await persist_diagnoses(db, run, summary)
    data = PlanRequest(
        diagnosisId=summary["diagnoses"][0]["record_id"], idempotencyKey="plan-key-123"
    )
    plan = await create_plan(db, run, data, "user-1")
    await db.commit()
    return run, plan, data


@pytest.mark.asyncio
async def test_plan_approval_signature_and_idempotency(db_session):
    run, plan, request = await fixture_plan(db_session)
    assert (await create_plan(db_session, run, request, "user-1")).id == plan.id
    alias = request.model_copy(update={"idempotency_key": "alias-key-123"})
    assert (await create_plan(db_session, run, alias, "user-1")).id == plan.id
    await db_session.commit()
    assert (await create_plan(db_session, run, alias, "user-1")).id == plan.id
    approval_request = ApprovalRequest(
        expectedRevision=1,
        reason="已检查原文，批准开展身份核实",
        idempotencyKey="approval-key-123",
        scope="investigation_plan",
    )
    approval = await approve_plan(
        db_session, plan, run, approval_request, "reviewer", "owner"
    )
    await db_session.commit()
    assert (
        await approve_plan(db_session, plan, run, approval_request, "reviewer", "owner")
    ).id == approval.id
    view = await plan_view(db_session, plan, run)
    assert view["approval_valid"] and not view["publication_allowed"]
    plan.title = "被改写的计划"
    assert (await plan_view(db_session, plan, run))["status"] == "stale"


@pytest.mark.asyncio
async def test_unrelated_refresh_does_not_invalidate_or_rewrite_approval(db_session):
    run, plan, _ = await fixture_plan(db_session)
    request = ApprovalRequest(
        expectedRevision=1,
        reason="已检查证据并开展核实",
        idempotencyKey="approval-key-123",
        scope="investigation_plan",
    )
    await approve_plan(db_session, plan, run, request, "reviewer", "owner")
    await db_session.commit()
    approval = await db_session.scalar(select(ActionApproval))
    original_signature = approval.signature
    await request_refresh(
        db_session, "p1", run.audit_id, "source_changed", "new-source"
    )
    assert (await plan_view(db_session, plan, run))["approval_valid"]
    assert approval.signature == original_signature


@pytest.mark.asyncio
async def test_model_configuration_change_invalidates_plan(db_session, monkeypatch):
    from app.config import settings

    run, plan, _ = await fixture_plan(db_session)
    request = ApprovalRequest(
        expectedRevision=1,
        reason="复核原文后批准计划",
        idempotencyKey="approval-key-123",
        scope="investigation_plan",
    )
    await approve_plan(db_session, plan, run, request, "reviewer", "owner")
    assert (await plan_view(db_session, plan, run))["approval_valid"]
    monkeypatch.setattr(settings, "analysis_llm_model", "a-different-model")
    assert (await plan_view(db_session, plan, run))["status"] == "stale"


@pytest.mark.asyncio
async def test_new_run_does_not_revoke_existing_plan_and_wrong_diagnosis_rejects(db_session):
    run, plan, request = await fixture_plan(db_session)
    with pytest.raises(HTTPException) as wrong:
        await create_plan(
            db_session,
            run,
            request.model_copy(update={"diagnosis_id": "f" * 32}),
            "user",
        )
    assert wrong.value.status_code == 404
    db_session.add(
        IntelligenceRun(
            project_id="p1",
            audit_id=run.audit_id,
            revision=run.revision + 1,
            input_hash="f" * 64,
            signature=run.signature,
            manifest=run.manifest,
            status="partial",
        )
    )
    await db_session.flush()
    await approve_plan(
            db_session,
            plan,
            run,
            ApprovalRequest(
                expectedRevision=1,
                reason="批准当前计划并核实",
                idempotencyKey="approval-key-123",
                scope="investigation_plan",
            ),
            "reviewer",
            "owner",
    )
    assert (await plan_view(db_session, plan, run))["approval_valid"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,project,actor,expected",
    [
        ("owner", "p1", "reviewer", 202),
        ("viewer", "p1", "reviewer", 403),
        ("owner", "p2", "reviewer", 404),
        ("owner", "p1", "", 403),
    ],
)
async def test_action_api_scope(
    client, db_session, monkeypatch, role, project, actor, expected
):
    from app.api.auth import get_current_user
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    _run, plan, _ = await fixture_plan(db_session)
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": project,
        "role": role,
        "sub": actor,
    }
    try:
        response = await client.post(
            f"/api/intelligence/actions/{plan.id}/approve",
            json={
                "expectedRevision": 1,
                "reason": "已核对证据批准计划",
                "idempotencyKey": "approval-key-123",
                "scope": "investigation_plan",
            },
        )
        assert response.status_code == expected
    finally:
        app.dependency_overrides.pop(get_current_user, None)
