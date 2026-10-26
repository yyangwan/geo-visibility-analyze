from datetime import UTC, timedelta

import pytest
from fastapi import HTTPException

from app.intelligence.actions import (
    ApprovalRequest,
    approve_plan,
)
from app.intelligence.drafts import DraftRequest, authorize_draft, export_brief
from app.intelligence.reviews import (
    WithdrawRequest,
    withdraw_fact,
)
from app.utils.timezone import utcnow


async def fixture_draft(db, tmp_path, monkeypatch, project="p1", *, capture_v2=False):
    from app.intelligence.action_revisions import RevisionRequest, revise_plan
    from app.intelligence.blob import LocalBlobStore
    from app.intelligence.owned_assets import (
        RegisterAsset,
        register_asset,
        verify_asset,
    )
    from app.intelligence.owned_facts import approve_owned_fact
    from tests.test_intelligence_action_revisions import task_plan
    from tests.test_intelligence_owned_assets import TEXT, approval, verification

    run, plan, pack = await task_plan(db, project, reviewed=True, capture_v2=capture_v2)
    store = LocalBlobStore(tmp_path)
    monkeypatch.setattr("app.intelligence.owned_assets.configured_store", lambda: store)
    asset = await register_asset(
        db,
        project,
        RegisterAsset(
            registryBrandId="own",
            title="云衡产品规格",
            text=TEXT,
            expectedVersion=0,
            idempotencyKey="draft-own-material",
        ),
        "reviewer",
        store,
    )
    await verify_asset(db, asset, verification(asset), "reviewer", store)
    fact = await approve_owned_fact(db, asset, approval(asset), "reviewer", store)
    edited = pack.model_copy(update={"own_fact_ids": (fact.id,)})
    await revise_plan(
        db,
        plan,
        RevisionRequest(
            expectedRevision=1,
            taskPack=edited,
            reason="选择与本任务范围一致的我方事实",
            idempotencyKey="draft-select-fact",
        ),
        "reviewer",
    )
    await approve_plan(
        db,
        plan,
        run,
        ApprovalRequest(
            expectedRevision=2,
            reason="逐条核对我方事实适用范围后开展实验",
            scope="investigation_plan",
            idempotencyKey="plan-approval-key",
        ),
        "reviewer",
        "owner",
    )
    await db.commit()
    request = DraftRequest(
        expectedVersion=0,
        expectedRevision=2,
        factIds=[fact.id],
        factsScopeReviewed=True,
        channel="wechat",
        title="云衡专业版实施范围说明",
        reason="已检查事实适用范围和平台证据",
        expiresAt=utcnow().replace(tzinfo=UTC) + timedelta(hours=1),
        idempotencyKey="draft-approval-key",
    )
    return run, run, plan, fact, request


@pytest.mark.asyncio
async def test_capture_v2_original_task_can_authorize_and_export_content_without_inventing_conditions(
    db_session, tmp_path, monkeypatch
):
    _, run, plan, _, request = await fixture_draft(
        db_session, tmp_path, monkeypatch, capture_v2=True
    )
    raw = run.manifest["responses"][0]["conditions"].copy()
    grant = await authorize_draft(
        db_session,
        plan,
        run,
        request.model_copy(update={"channel": "website"}),
        "reviewer",
    )
    await db_session.commit()
    brief = await export_brief(db_session, grant, plan, run)
    actual = brief["aiTargets"][0]["observations"][0]["conditions"]
    assert actual == {
        "surface": "app",
        "search_mode": "unknown",
        "model_version": None,
        "account_class": None,
        "location": None,
    }
    assert run.manifest["responses"][0]["conditions"] == raw
    assert "searchMode" in raw and "searchMode" not in actual


def test_brief_condition_adapter_keeps_observed_values_and_never_uses_requested_conditions():
    from app.intelligence.drafts import brief_conditions

    response = {
        "prompt_version_id": "frozen",
        "surface": "app",
        "conditions": {
            "searchMode": "disabled",
            "modelVersion": "observed-model",
            "accountClass": None,
            "location": None,
        },
        "requested_conditions": {
            "searchMode": "enabled",
            "modelVersion": "requested-model",
            "accountClass": "premium",
            "location": "Shanghai",
        },
    }
    assert brief_conditions(response) == {
        "surface": "app",
        "search_mode": "disabled",
        "model_version": "observed-model",
        "account_class": None,
        "location": None,
    }


@pytest.mark.asyncio
async def test_draft_approval_replay_and_manual_website_dispatch(
    db_session, tmp_path, monkeypatch
):
    _, run, plan, fact, request = await fixture_draft(db_session, tmp_path, monkeypatch)
    request = request.model_copy(update={"channel": "website"})
    grant = await authorize_draft(db_session, plan, run, request, "reviewer")
    await db_session.commit()
    assert (
        await authorize_draft(db_session, plan, run, request, "reviewer")
    ).id == grant.id
    brief = await export_brief(db_session, grant, plan, run)
    assert (
        brief["schemaVersion"] == 3
        and brief["ownFacts"][0]["statement"] == fact.statement
    )
    assert brief["execution"] == {"channel": "website", "mode": "manual_task"}
    assert (
        brief["aiTargets"][0]["platform"] == "qwen" and not brief["publicationAllowed"]
    )


@pytest.mark.asyncio
async def test_fact_withdrawal_and_payload_tamper_prevent_export(
    db_session, tmp_path, monkeypatch
):
    origin, run, plan, fact, request = await fixture_draft(
        db_session, tmp_path, monkeypatch
    )
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
    tmp_path,
    monkeypatch,
):
    _, run, plan, _, request = await fixture_draft(db_session, tmp_path, monkeypatch)
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
    client, db_session, monkeypatch, tmp_path, role, project, expected
):
    from app.api.auth import get_current_user
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    _, _, plan, _, request = await fixture_draft(db_session, tmp_path, monkeypatch)
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
            assert (
                await client.get(
                    f"/api/intelligence/draft-approvals/{approval_id}/brief"
                )
            ).status_code == 200
            monkeypatch.setattr(settings, "intelligence_enabled", False)
            assert (
                await client.get(
                    f"/api/intelligence/draft-approvals/{approval_id}/brief"
                )
            ).status_code == 503
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_content_authority_is_exact_and_not_revoked_by_unrelated_context(
    db_session, tmp_path, monkeypatch
):
    from app.intelligence.action_revisions import RevisionRequest, revise_plan
    from app.intelligence.models import IntelligenceRun
    from app.intelligence.project_context import ContextRequest, receive_context

    _, run, plan, fact, request = await fixture_draft(db_session, tmp_path, monkeypatch)
    grant = await authorize_draft(db_session, plan, run, request, "reviewer")
    await db_session.commit()
    await receive_context(
        db_session,
        "p1",
        ContextRequest(
            version=2,
            brands=[
                {"id": "own", "name": "云衡", "is_competitor": False},
                {"id": "unrelated", "name": "无关竞品", "is_competitor": True},
            ],
        ),
        "reviewer",
    )
    db_session.add(
        IntelligenceRun(
            project_id="p1",
            audit_id=run.audit_id,
            revision=run.revision + 1,
            input_hash="e" * 64,
            signature=run.signature,
            manifest=run.manifest,
            status="partial",
        )
    )
    await db_session.flush()
    brief = await export_brief(db_session, grant, plan, run)
    assert brief["ownFacts"][0]["id"] == fact.id
    assert brief["runId"] == run.id and brief["actionRevision"]["revision"] == 2
    assert brief["taskPack"] == plan.target["task_pack"]
    await revise_plan(
        db_session,
        plan,
        RevisionRequest(
            expectedRevision=2,
            title="调整内容实验的具体交付范围",
            reason="负责人要求重新核对内容交付范围",
            idempotencyKey="new-title-revision",
        ),
        "reviewer",
    )
    with pytest.raises(HTTPException) as stale:
        await export_brief(db_session, grant, plan, run)
    assert stale.value.status_code == 409
    assert grant.payload["actionRevision"]["revision"] == 2


@pytest.mark.asyncio
async def test_fact_qualification_is_shared_and_rejects_forged_sources(
    db_session, tmp_path, monkeypatch
):
    from app.intelligence.fact_qualification import eligible_facts, qualified_fact
    from app.intelligence.reviews import fact_view

    _, _, plan, fact, _ = await fixture_draft(db_session, tmp_path, monkeypatch)
    page = await eligible_facts(db_session, plan, 2)
    assert page["items"][0]["eligible"] and page["items"][0]["selected"]
    assert page["items"][0]["semantic_fit"] == "requires_named_review"
    assert await fact_view(db_session, fact) == await qualified_fact(
        db_session, "p1", fact.id
    )
    fact.source_ref = {**fact.source_ref, "verification_id": "not-the-approved-source"}
    await db_session.flush()
    changed = await eligible_facts(db_session, plan, 2)
    assert not changed["items"][0]["eligible"]
    assert changed["projection_hash"] != page["projection_hash"]
    assert changed["items"][0]["status"] == "needs_review"
    with pytest.raises(HTTPException) as stale:
        await eligible_facts(db_session, plan, 1)
    assert stale.value.status_code == 409


@pytest.mark.asyncio
async def test_content_requires_task_selected_facts_and_named_scope_review(
    db_session, tmp_path, monkeypatch
):
    _, run, plan, _, request = await fixture_draft(db_session, tmp_path, monkeypatch)
    for change, status in [
        ({"facts_scope_reviewed": False}, 422),
        ({"expected_revision": 1}, 409),
        ({"fact_ids": ["foreign-fact"]}, 409),
    ]:
        with pytest.raises(HTTPException) as failure:
            await authorize_draft(
                db_session, plan, run, request.model_copy(update=change), "reviewer"
            )
        assert failure.value.status_code == status


@pytest.mark.asyncio
async def test_fact_and_authorization_reads_are_scoped_and_read_only(
    client, db_session, tmp_path, monkeypatch
):
    from sqlalchemy import func, select

    from app.api.auth import get_current_user
    from app.config import settings
    from app.intelligence.models import DraftApproval
    from app.main import app

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    _, _, plan, _, _ = await fixture_draft(db_session, tmp_path, monkeypatch)
    try:
        for project, status in [("p1", 200), ("p2", 404)]:
            app.dependency_overrides[get_current_user] = lambda project=project: {
                "scope": "project",
                "pid": project,
                "role": "viewer",
                "sub": "reader",
            }
            for path in ("eligible-facts", "draft-approvals"):
                result = await client.get(
                    f"/api/intelligence/actions/{plan.id}/{path}?expected_revision=2"
                )
                assert result.status_code == status
                if path == "draft-approvals" and status == 200:
                    assert (
                        result.json()["latest_version"] == 0
                        and not result.json()["can_authorize"]
                    )
        assert (
            await db_session.scalar(select(func.count()).select_from(DraftApproval))
            == 0
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
