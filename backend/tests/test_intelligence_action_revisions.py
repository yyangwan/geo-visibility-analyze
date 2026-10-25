"""Immutable actionable plans retain exact scope, history and relevant approvals."""

from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.action_revisions import (
    RevisionRequest,
    current_revision,
    revise_plan,
    revision_history,
)
from app.intelligence.actions import (
    ApprovalRequest,
    PlanRequest,
    approve_plan,
    create_plan,
    persist_diagnoses,
    plan_view,
)
from app.intelligence.blob import digest
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    AnalysisJob,
    ContentSegment,
    EntityResolutionEvent,
    IntelligenceEntity,
    IntelligenceRun,
)
from app.intelligence.project_context import ContextRequest, receive_context
from app.intelligence.semantics import stable_hash
from app.intelligence.value_contracts import TaskPackV2
from app.main import app
from tests.test_intelligence_foundation import fixture_audit


async def task_plan(db, project="p1", *, fact_ids=(), own_id="own", reviewed=False):
    await receive_context(
        db,
        project,
        ContextRequest(
            version=1,
            brands=[
                {"id": own_id, "name": "云衡", "is_competitor": False},
            ],
        ),
        "owner",
    )
    audit = await fixture_audit(
        db, project=project, text="🧭方案需要列明实施条件和服务范围。"
    )
    run = await prepare_run(db, project, audit.id, enrich=False)
    run.status = "partial"
    response = run.manifest["responses"][0]
    if reviewed:
        from app.intelligence.brand_evaluation import freeze_cohort
        from app.intelligence.evaluation_reviews import reviewed_cohort
        from app.intelligence.question_context import QuestionReview

        base = freeze_cohort(
            [
                {
                    "sample_id": response["id"],
                    "sample_index": 1,
                    "platform": response["platform"],
                    "prompt_hash": response["prompt_hash"],
                    "prompt": response["prompt"],
                    "category": "scenario",
                },
                {
                    "sample_id": "unrelated",
                    "sample_index": 1,
                    "platform": "qwen",
                    "prompt_hash": stable_hash("其他问题"),
                    "prompt": "其他问题",
                    "category": "scenario",
                },
            ],
            run.manifest["brands"],
        )
        reviews = {
            row["prompt_hash"]: QuestionReview(
                status="confirmed",
                primaryScenario="implementation",
                brandScope="neutral",
            )
            for row in base["cells"]
        }
        cohort = reviewed_cohort(base, {key: "eligible" for key in reviews}, reviews)
        run.manifest = {
            **run.manifest,
            "evaluation_base_cohort": base,
            "evaluation_cohort": cohort,
        }
        run.input_hash = stable_hash(run.manifest)
    segment = ContentSegment(
        project_id=project,
        owner_kind="answer",
        owner_id=response["id"],
        ordinal=0,
        start_offset=0,
        end_offset=len(response["text"]),
        text=response["text"],
        segment_hash=stable_hash(response["text"]),
        extractor_version="revision-test",
    )
    db.add(segment)
    await db.flush()
    pack = TaskPackV2.model_validate(
        {
            "project_id": project,
            "own_target_id": "registry:" + own_id,
            "asset_key": "new:implementation-guide",
            "fact_change_key": "implementation-preconditions",
            "intent": "content_create",
            "deliverable": "编写服务范围与实施前提说明",
            "delivery": {
                "asset_title": "实施前提说明",
                "asset_kind": "new_deliverable",
                "audience_status": "unknown",
                "outline": [
                    "列出已核实的服务范围及实施条件",
                    "为每项限定提供材料原文与有效日期",
                ],
                "channel": "own_website",
                "channel_reason": "便于客户核查并引用自身材料",
                "questions": [
                    {"prompt_hash": response["prompt_hash"], "text": response["prompt"]}
                ],
            },
            "acceptance": ["限定条件和事实均有明确材料及范围"],
            "ai_targets": [
                {
                    "platform": response["platform"],
                    "prompt_hash": response["prompt_hash"],
                    "scenario_id": "implementation" if reviewed else "unreviewed",
                    "dimensions": ["reasons"],
                }
            ],
            "diagnostic_evidence": [
                {
                    "project_id": project,
                    "run_id": run.id,
                    "owner_kind": "answer",
                    "owner_id": response["id"],
                    "segment_id": segment.id,
                    "text_hash": digest(response["text"].encode()),
                    "quote": response["text"],
                    "start": 0,
                    "end": len(response["text"]),
                }
            ],
            "own_fact_ids": fact_ids,
            "missing_materials": ["需要核实服务范围"],
        }
    )
    diagnosis = {
        "id": stable_hash([run.id, pack.task_key()]),
        "task_key": pack.task_key(),
        "task_signature": pack.revision_signature(),
        "task_pack": pack.model_dump(mode="json"),
        "platform": response["platform"],
        "prompt_hash": response["prompt_hash"],
        "prompt": response["prompt"],
        "response_ids": [response["id"]],
        "source_ids": [],
        "evidence_ids": [],
        "action": {"task": pack.deliverable},
    }
    await persist_diagnoses(db, run, {"diagnoses": [diagnosis]})
    action = await create_plan(
        db,
        run,
        PlanRequest(
            diagnosisId=diagnosis["record_id"], idempotencyKey="initial-task-plan"
        ),
        "owner",
    )
    await db.commit()
    return run, action, pack


def revision_request(**changes):
    return RevisionRequest(
        **{
            "expectedRevision": 1,
            "title": "实施前提及服务范围说明（已修订）",
            "reason": "按负责人要求补充交付验收范围",
            "idempotencyKey": "revision-request-1",
            **changes,
        }
    )


def approval_request(revision=1, key="approve-revision-1"):
    return ApprovalRequest(
        expectedRevision=revision,
        reason="逐条核对任务证据、交付与适用范围",
        scope="investigation_plan",
        idempotencyKey=key,
    )


@pytest.mark.asyncio
async def test_revision_history_never_reuses_approval_and_retries_are_exact(db_session):
    run, action, pack = await task_plan(db_session)
    original = deepcopy(action.target)
    first = await approve_plan(
        db_session, action, run, approval_request(), "owner", "owner"
    )
    await db_session.commit()
    old_signature = first.signature
    edited = pack.model_copy(update={"deliverable": "编写附实施验收清单的服务范围说明"})
    request = revision_request(taskPack=edited.model_dump(mode="json"))
    second = await revise_plan(db_session, action, request, "editor")
    await db_session.commit()
    assert second.revision == 2 and action.status == "proposed"
    assert not (await plan_view(db_session, action, run))["approval_valid"]
    assert (await revise_plan(db_session, action, request, "editor")).id == second.id
    with pytest.raises(HTTPException) as conflict:
        await revise_plan(
            db_session,
            action,
            request.model_copy(update={"reason": "不同请求不得复用同一个幂等编号"}),
            "editor",
        )
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException):
        await approve_plan(
            db_session,
            action,
            run,
            approval_request(key="stale-revision-approval"),
            "owner",
            "owner",
        )
    # Replay identifies the old receipt; it does not approve the new pointer.
    assert (
        await approve_plan(
            db_session, action, run, approval_request(), "owner", "owner"
        )
    ).id == first.id
    assert action.status == "proposed"
    await approve_plan(
        db_session,
        action,
        run,
        approval_request(2, "approve-revision-2"),
        "owner",
        "owner",
    )
    await db_session.commit()
    history = await revision_history(db_session, action)
    assert [row["revision"] for row in history] == [2, 1]
    assert (
        history[1]["payload"]["target"] == original and first.signature == old_signature
    )
    assert all(row["approval"]["historical_signature_valid"] for row in history)
    assert any(row["field"] == "/title" for row in history[0]["changes_from_previous"]["task"])
    assert (await plan_view(db_session, action, run))["approval_valid"]
    assert (
        history[0]["payload"]["target"]["task_pack"]["delivery"]["questions"]
        == original["task_pack"]["delivery"]["questions"]
    )


@pytest.mark.asyncio
async def test_unrelated_brand_preferences_identity_and_new_run_keep_approval(
    db_session,
):
    run, action, _ = await task_plan(db_session)
    await approve_plan(db_session, action, run, approval_request(), "owner", "owner")
    await db_session.commit()
    await receive_context(
        db_session,
        "p1",
        ContextRequest(
            version=2,
            brands=[
                {"id": "own", "name": "云衡", "is_competitor": False},
                {"id": "unrelated", "name": "无关品牌", "is_competitor": True},
            ],
            business_preferences={
                "version": "business-preferences-v1",
                "scenario_importance": {"implementation": 90},
                "reason": "本季度优先处理实施问题",
            },
        ),
        "owner",
    )
    other = IntelligenceEntity(
        project_id="p1", name="独立实体", kind="brand", identity_hash="c" * 64
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        EntityResolutionEvent(
            project_id="p1",
            entity_id=other.id,
            run_id=run.id,
            version=1,
            decision="confirmed",
            canonical_name="独立实体",
            reason="与当前任务没有原文关联",
            actor_id="owner",
            idempotency_key="unrelated-identity",
            request_hash="b" * 64,
        )
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
    view = await plan_view(db_session, action, run)
    assert view["approval_valid"] and not view["dependency_changes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rename", "collision", "competitor", "remove"])
async def test_related_registry_change_returns_diff_and_cannot_resign_old_interpretation(
    db_session, change
):
    run, action, _ = await task_plan(db_session)
    await approve_plan(db_session, action, run, approval_request(), "owner", "owner")
    brands = [
        {
            "id": "own",
            "name": "另一品牌" if change == "rename" else "云衡",
            "is_competitor": change == "competitor",
        }
    ]
    if change == "collision":
        brands.append({"id": "collision", "name": "云衡", "is_competitor": True})
    if change == "remove":
        brands = []
    await receive_context(
        db_session, "p1", ContextRequest(version=2, brands=brands), "owner"
    )
    view = await plan_view(db_session, action, run)
    assert view["status"] == "stale" and not view["approval_valid"]
    assert any(row["dependency"] == "identity" for row in view["dependency_changes"])
    with pytest.raises(HTTPException) as refused:
        await revise_plan(db_session, action, revision_request(), "owner")
    assert refused.value.status_code == 409
    assert action.revision == 1


@pytest.mark.asyncio
async def test_changed_exact_evidence_and_revision_payload_are_detected(db_session):
    run, action, pack = await task_plan(db_session)
    await approve_plan(db_session, action, run, approval_request(), "owner", "owner")
    segment = await db_session.get(
        ContentSegment, pack.diagnostic_evidence[0].segment_id
    )
    original = segment.text
    segment.text = "🧭被修改的原文不能继续使用旧批准。"
    assert (await plan_view(db_session, action, run))["status"] == "stale"
    segment.text = original
    assert (await plan_view(db_session, action, run))["approval_valid"]
    row, _ = await current_revision(db_session, action)
    row.payload = {**row.payload, "title": "直接覆盖不可变载荷"}
    assert (await plan_view(db_session, action, run))["status"] == "stale"
    assert not (await revision_history(db_session, action))[0]["integrity_valid"]


@pytest.mark.asyncio
async def test_diagnostic_scope_cannot_be_forged_or_truncated(db_session):
    _run, action, pack = await task_plan(db_session)
    altered = pack.model_dump(mode="json")
    altered["diagnostic_evidence"][0]["run_id"] = uuid4().hex
    with pytest.raises(HTTPException) as wrong:
        await revise_plan(
            db_session, action, revision_request(taskPack=altered), "owner"
        )
    assert wrong.value.status_code == 422
    with pytest.raises(HTTPException):
        await revise_plan(
            db_session, action, revision_request(title=action.title), "owner"
        )
    assert action.revision == 1


@pytest.mark.asyncio
async def test_rebase_same_task_to_published_run_preserves_previous_revision(
    db_session,
):
    run, action, _ = await task_plan(db_session)
    await approve_plan(db_session, action, run, approval_request(), "owner", "owner")
    newer = IntelligenceRun(
        project_id="p1",
        audit_id=run.audit_id,
        revision=run.revision + 1,
        input_hash="e" * 64,
        signature=run.signature,
        manifest=deepcopy(run.manifest),
        status="partial",
    )
    db_session.add(newer)
    await db_session.flush()
    target = deepcopy(action.target)
    target["id"] = stable_hash([newer.id, target["task_key"]])
    for ref in target["task_pack"]["diagnostic_evidence"]:
        ref["run_id"] = newer.id
    target["task_signature"] = TaskPackV2.model_validate(
        target["task_pack"]
    ).revision_signature()
    await persist_diagnoses(db_session, newer, {"diagnoses": [target]})
    await revise_plan(
        db_session, action, revision_request(diagnosisId=target["record_id"]), "owner"
    )
    await db_session.commit()
    current, current_run = await current_revision(db_session, action)
    assert current_run.id == newer.id and action.run_id == run.id
    assert current.task_key == action.target["task_key"]
    assert not (await plan_view(db_session, action, newer))["approval_valid"]
    await approve_plan(
        db_session,
        action,
        newer,
        approval_request(2, "rebase-approval-2"),
        "owner",
        "owner",
    )
    history = await revision_history(db_session, action)
    assert history[1]["run_id"] == run.id and history[0]["run_id"] == newer.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,project,actor,expected",
    [
        ("owner", "p1", "owner", 202),
        ("viewer", "p1", "viewer", 403),
        ("owner", "p2", "owner", 404),
        ("owner", "p1", "", 403),
    ],
)
async def test_revision_api_permissions_history_and_get_is_read_only(
    db_session, client, monkeypatch, role, project, actor, expected
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    _run, action, _ = await task_plan(db_session)
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": project,
        "sub": actor,
        "role": role,
    }
    try:
        response = await client.post(
            f"/api/intelligence/actions/{action.id}/revisions",
            json=revision_request().model_dump(mode="json", by_alias=True),
        )
        assert response.status_code == expected
        if expected == 202:
            jobs = len((await db_session.scalars(select(AnalysisJob))).all())
            history = await client.get(
                f"/api/intelligence/actions/{action.id}/revisions?offset=1&limit=1"
            )
            assert (
                history.status_code == 200
                and history.json()["revisions"][0]["revision"] == 1
            )
            detail = await client.get(f"/api/intelligence/actions/{action.id}")
            assert detail.status_code == 200 and detail.json()["revision"] == 2
            assert len((await db_session.scalars(select(AnalysisJob))).all()) == jobs
            assert (
                await client.get(
                    f"/api/intelligence/actions/{action.id}/revisions?limit=101"
                )
            ).status_code == 422
        if project != "p1":
            assert (
                await client.get(f"/api/intelligence/actions/{action.id}/revisions")
            ).status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_selected_own_fact_withdrawal_invalidates_only_related_task(
    db_session, tmp_path
):
    from app.intelligence.blob import LocalBlobStore
    from app.intelligence.owned_assets import register_asset, verify_asset
    from app.intelligence.owned_facts import approve_owned_fact, withdraw_owned_fact
    from app.intelligence.reviews import WithdrawRequest
    from tests.test_intelligence_owned_assets import (
        approval,
        registration,
        verification,
    )

    run, action, pack = await task_plan(db_session, own_id="b1")
    await approve_plan(db_session, action, run, approval_request(), "owner", "owner")
    store = LocalBlobStore(tmp_path)
    asset = await register_asset(db_session, "p1", registration(), "owner", store)
    await verify_asset(db_session, asset, verification(asset), "owner", store)
    fact = await approve_owned_fact(db_session, asset, approval(asset), "owner", store)
    assert (await plan_view(db_session, action, run))["approval_valid"]
    edited = pack.model_copy(update={"own_fact_ids": (fact.id,)})
    await revise_plan(
        db_session,
        action,
        revision_request(taskPack=edited.model_dump(mode="json")),
        "owner",
    )
    await approve_plan(
        db_session,
        action,
        run,
        approval_request(2, "own-fact-plan-2"),
        "owner",
        "owner",
    )
    await db_session.commit()
    assert (await plan_view(db_session, action, run))["approval_valid"]
    await withdraw_owned_fact(
        db_session,
        fact,
        WithdrawRequest(
            expectedVersion=1,
            reason="材料范围需重新确认后方可使用",
            idempotencyKey="withdraw-selected-fact",
        ),
        "owner",
    )
    view = await plan_view(db_session, action, run)
    assert not view["approval_valid"]
    assert any(
        row["dependency"] == "fact:" + fact.id for row in view["dependency_changes"]
    )
    history = await revision_history(db_session, action)
    assert history[1]["approval"]["historical_signature_valid"]
    # Removing the withdrawn fact is an explicit new revision, never an in-place renewal.
    await revise_plan(
        db_session,
        action,
        revision_request(
            expectedRevision=2,
            taskPack=pack.model_dump(mode="json"),
            idempotencyKey="remove-invalid-fact",
        ),
        "owner",
    )
    assert (
        action.revision == 3
        and not (await plan_view(db_session, action, run))["approval_valid"]
    )


@pytest.mark.asyncio
async def test_old_draft_does_not_revive_after_new_plan_is_approved(db_session, tmp_path, monkeypatch):
    from app.intelligence.drafts import authorize_draft, export_brief
    from tests.test_intelligence_drafts import fixture_draft

    _, run, action, _, request = await fixture_draft(db_session, tmp_path, monkeypatch)
    grant = await authorize_draft(db_session, action, run, request, "reviewer")
    await db_session.commit()
    await revise_plan(db_session, action, revision_request(expectedRevision=2), "owner")
    await approve_plan(
        db_session,
        action,
        run,
        approval_request(3, "approve-revised-draft-plan"),
        "owner",
        "owner",
    )
    await db_session.commit()
    with pytest.raises(HTTPException) as old:
        await export_brief(db_session, grant, action, run)
    assert old.value.status_code == 409
    history = await revision_history(db_session, action)
    assert (
        history[1]["draft_approval_ids"] == [grant.id]
        and not history[0]["draft_approval_ids"]
    )


@pytest.mark.asyncio
async def test_only_target_question_review_invalidates_plan(db_session):
    from app.intelligence.evaluation_reviews import reviewed_cohort
    from app.intelligence.models import EvaluationCohortReview
    from app.intelligence.question_context import QuestionReview

    run, action, pack = await task_plan(db_session, reviewed=True)
    await approve_plan(db_session, action, run, approval_request(), "owner", "owner")
    base = run.manifest["evaluation_base_cohort"]
    reviews = {
        row["prompt_hash"]: QuestionReview(
            status="confirmed", primaryScenario="implementation", brandScope="neutral"
        )
        for row in base["cells"]
    }
    reviews[stable_hash("其他问题")] = QuestionReview(
        status="confirmed", primaryScenario="risk", brandScope="neutral"
    )
    cohort = reviewed_cohort(base, {key: "eligible" for key in reviews}, reviews)
    db_session.add(
        EvaluationCohortReview(
            project_id="p1",
            audit_id=run.audit_id,
            run_id=run.id,
            version=1,
            base_hash=base["hash"],
            cohort=cohort,
            reason="只改变不相关问题的业务场景",
            actor_id="owner",
            idempotency_key="unrelated-question-review",
            request_hash="f" * 64,
        )
    )
    await db_session.flush()
    assert (await plan_view(db_session, action, run))["approval_valid"]
    reviews[pack.ai_targets[0].prompt_hash] = QuestionReview(
        status="confirmed", primaryScenario="risk", brandScope="neutral"
    )
    db_session.add(
        EvaluationCohortReview(
            project_id="p1",
            audit_id=run.audit_id,
            run_id=run.id,
            version=2,
            base_hash=base["hash"],
            cohort=reviewed_cohort(base, {key: "eligible" for key in reviews}, reviews),
            reason="当前目标问题需要改为风险场景",
            actor_id="owner",
            idempotency_key="relevant-question-review",
            request_hash="e" * 64,
        )
    )
    await db_session.flush()
    view = await plan_view(db_session, action, run)
    assert not view["approval_valid"] and any(
        row["dependency"] == "questions" for row in view["dependency_changes"]
    )


@pytest.mark.asyncio
async def test_same_snapshot_fetch_is_stable_and_changed_related_source_is_stale(
    db_session,
):
    from app.intelligence.models import SourceFetch, SourceSnapshot
    from tests.test_intelligence_reviews import fixture_evidence

    run, evidence, _, _ = await fixture_evidence(db_session)
    summary = {"diagnoses": [{"id": "a" * 64, "platform": "qwen", "prompt": "核对质保条件",
        "response_ids": [run.manifest["responses"][0]["id"]], "source_ids": [run.manifest["sources"][0]["id"]],
        "evidence_ids": [evidence.id], "action": {"task": "核实质保条件原文"}}]}
    await persist_diagnoses(db_session, run, summary)
    action = await create_plan(db_session, run, PlanRequest(diagnosisId=summary["diagnoses"][0]["record_id"], idempotencyKey="source-plan-123"), "owner")
    await approve_plan(db_session, action, run, approval_request(), "owner", "owner")
    source = run.manifest["sources"][0]
    db_session.add(
        SourceFetch(
            project_id="p1",
            audit_id=run.audit_id,
            document_id=source["document_id"],
            snapshot_id=source["snapshot_id"],
            status="captured",
        )
    )
    await db_session.flush()
    assert (await plan_view(db_session, action, run))["approval_valid"]
    snapshot = await db_session.get(SourceSnapshot, source["snapshot_id"])
    snapshot.integrity_status = "quarantined"
    view = await plan_view(db_session, action, run)
    assert not view["approval_valid"] and any(
        row["dependency"] == "source:" + source["id"]
        for row in view["dependency_changes"]
    )
    snapshot.integrity_status = "verified"
    snapshot.text_key = "different-text-object.gz"
    assert not (await plan_view(db_session, action, run))["approval_valid"]


def test_differently_named_registry_binding_to_used_entity_is_a_related_change():
    from app.intelligence.action_dependencies import identity_projection

    brand = {"id": "own", "name": "云衡", "entity_id": "used-entity"}
    scope = {"entity_ids": ["used-entity"], "target_ids": ["registry:own"], "binding_keys": []}
    before = identity_projection({"brands": [brand]}, {}, {}, {}, scope)
    after = identity_projection({"brands": [brand, {"id": "rival", "name": "完全不同的名称", "entity_id": "used-entity"}]}, {}, {}, {}, scope)
    assert before != after and after["name_collisions"][0]["id"] == "rival"
