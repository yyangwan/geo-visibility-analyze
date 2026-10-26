"""Named execution cannot inherit another revision's authority, delivery or owner."""

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.intelligence.action_revisions import revise_plan
from app.intelligence.actions import approve_plan
from app.intelligence.execution import (
    ExecutionRequest,
    change_execution,
    execution_events,
    read_execution,
)
from app.intelligence.models import ActionExecution, ActionExecutionEvent
from app.intelligence.project_context import ContextRequest, receive_context
from app.intelligence.semantics import stable_hash
from tests.test_intelligence_action_revisions import (
    approval_request,
    revision_request,
    task_plan,
)


def command(operation, version, **fields):
    return ExecutionRequest(
        expectedRevision=1,
        expectedVersion=version,
        operation=operation,
        reason="按本修订原问题与验收标准核对后操作",
        idempotencyKey=f"execution-{operation}-{version}",
        **fields,
    )


def principal(action, data, *, actor="owner", role="owner", **proof):
    wire = stable_hash(data.model_dump(mode="json", by_alias=True, exclude_unset=True))
    return {
        "sub": actor,
        "pid": action.project_id,
        "scope": "project",
        "role": role,
        "execution_authority": {
            "action_id": action.id,
            "revision": data.expected_revision,
            "request_hash": wire,
            "current_owner_id": "owner",
            **proof,
        },
    }, wire


async def change(db, action, data, **options):
    user, wire = principal(action, data, **options)
    value = await change_execution(db, action, data, user, wire)
    await db.commit()
    return value


async def arranged(db, *, approved=True):
    run, action, pack = await task_plan(db)
    if approved:
        await approve_plan(db, action, run, approval_request(), "owner", "owner")
        await db.commit()
    await change(
        db,
        action,
        command("assign", 0, ownerId="owner", dueAt="2026-11-01T10:00:00Z"),
        owner={"id": "owner", "name": "内容负责人", "role": "owner"},
    )
    return run, action, pack


@pytest.mark.asyncio
async def test_read_does_not_initialize_and_follows_the_exact_revision(db_session):
    _, action, _ = await task_plan(db_session)
    view = await read_execution(db_session, action, 1)
    assert view["version"] == 0 and view["snapshot"]["status"] == "not_started"
    assert not view["plan_approval_valid"]
    assert not db_session.new and not await db_session.scalar(
        select(func.count()).select_from(ActionExecution)
    )
    assert (await execution_events(db_session, action, 1))["events"] == []


@pytest.mark.asyncio
async def test_read_rechecks_current_plan_dependencies_without_execution_write(db_session):
    _, action, _ = await arranged(db_session)
    assert (await read_execution(db_session, action, 1))["plan_approval_valid"]
    await receive_context(
        db_session,
        action.project_id,
        ContextRequest(
            version=2,
            brands=[{"id": "own", "name": "另一品牌", "is_competitor": False}],
        ),
        "owner",
    )
    view = await read_execution(db_session, action, 1)
    assert not view["plan_approval_valid"] and view["version"] == 1
    assert view["snapshot"]["owner"]["id"] == "owner"
    assert await db_session.scalar(select(func.count()).select_from(ActionExecutionEvent)) == 1


@pytest.mark.asyncio
async def test_named_delivery_return_acceptance_and_exact_retry(db_session):
    _, action, pack = await arranged(db_session)
    start = command("start", 1)
    assert (await change(db_session, action, start))["snapshot"][
        "status"
    ] == "in_progress"
    assert (await change(db_session, action, start))["replayed"]
    artifacts = [
        {
            "kind": "external_url",
            "title": "实施范围与条件说明",
            "url": "https://example.com/guide",
        }
    ]
    delivery = await change(
        db_session, action, command("deliver", 2, artifacts=artifacts)
    )
    delivery_id = delivery["snapshot"]["delivery"]["event_id"]
    assert (
        delivery["snapshot"]["delivery"]["artifacts"][0]["verification"]
        == "manual_registration"
    )
    returned = await change(
        db_session,
        action,
        command(
            "return",
            3,
            acceptanceChecks=[{"index": 0, "met": False, "note": "需要补充限定范围"}],
        ),
    )
    assert returned["snapshot"]["status"] == "in_progress"
    assert returned["snapshot"]["acceptance"]["delivery_event_id"] == delivery_id
    await change(db_session, action, command("deliver", 4, artifacts=artifacts))
    accepted = await change(
        db_session,
        action,
        command(
            "accept",
            5,
            acceptanceChecks=[
                {"index": i, "met": True} for i in range(len(pack.acceptance))
            ],
        ),
    )
    assert (
        accepted["snapshot"]["status"] == "accepted"
        and not accepted["publication_allowed"]
    )
    assert accepted["snapshot"]["delivery"]["event_id"] != delivery_id
    assert (
        accepted["snapshot"]["acceptance"]["checks"][0]["criterion"]
        == pack.acceptance[0]
    )
    history = await execution_events(db_session, action, 1, limit=2)
    assert [r["version"] for r in history["events"]] == [6, 5] and history[
        "next_before"
    ] == 4
    assert all(r["integrity_valid"] for r in history["events"])


@pytest.mark.asyncio
async def test_material_preparation_does_not_grant_execution_authority(db_session):
    _, action, _ = await arranged(db_session, approved=False)
    await change(db_session, action, command("prepare", 1))
    with pytest.raises(HTTPException) as failure:
        await change(db_session, action, command("start", 2))
    assert failure.value.status_code == 409
    assert (await read_execution(db_session, action, 1))["snapshot"][
        "status"
    ] == "preparing_materials"


@pytest.mark.asyncio
async def test_revision_change_preserves_old_delivery_but_starts_fresh(db_session):
    _, action, _ = await arranged(db_session)
    start = command("start", 1)
    await change(db_session, action, start)
    await revise_plan(db_session, action, revision_request(), "owner")
    await db_session.commit()
    new = await read_execution(db_session, action, 2)
    assert (
        new["version"] == 0
        and new["snapshot"]["owner"] is None
        and new["snapshot"]["delivery"] is None
    )
    old = await read_execution(db_session, action, 1)
    assert not old["is_current"] and old["snapshot"]["status"] == "in_progress"
    assert not old["plan_approval_valid"] and not new["plan_approval_valid"]
    assert (await change(db_session, action, start))["replayed"]
    with pytest.raises(HTTPException) as failure:
        await change(db_session, action, command("cancel", 2))
    assert failure.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["missing_proof", "body_hash", "foreign_action", "viewer", "member_assign_other"],
)
async def test_untrusted_execution_scope_or_assignment_is_rejected(
    db_session, mutation
):
    _, action, _ = await task_plan(db_session)
    data = command("assign", 0, ownerId="owner", dueAt=None)
    user, wire = principal(
        action, data, owner={"id": "owner", "name": "内容负责人", "role": "owner"}
    )
    if mutation == "missing_proof":
        user.pop("execution_authority")
    if mutation == "body_hash":
        user["execution_authority"]["request_hash"] = "0" * 64
    if mutation == "foreign_action":
        user["execution_authority"]["action_id"] = "other"
    if mutation == "viewer":
        user["role"] = "viewer"
    if mutation == "member_assign_other":
        user.update(role="member", sub="member")
    with pytest.raises(HTTPException) as failure:
        await change_execution(db_session, action, data, user, wire)
    assert failure.value.status_code == 403
    assert not await db_session.scalar(
        select(func.count()).select_from(ActionExecution)
    )


@pytest.mark.asyncio
async def test_version_cas_and_material_changes_block_delivery(db_session):
    _, action, _ = await arranged(db_session)
    with pytest.raises(HTTPException) as failure:
        await change(db_session, action, command("start", 0))
    assert failure.value.status_code == 409
    await change(db_session, action, command("start", 1))
    action.target = {**action.target, "action": {"task": "非法覆盖当前行动的任务内容"}}
    with pytest.raises(HTTPException) as failure:
        await change(
            db_session,
            action,
            command(
                "deliver",
                2,
                artifacts=[
                    {
                        "kind": "manual_record",
                        "title": "核实材料范围记录",
                        "notes": "已核对原文与适用范围",
                    }
                ],
            ),
        )
    assert failure.value.status_code == 409


@pytest.mark.asyncio
async def test_incomplete_acceptance_and_foreign_service_artifact_do_not_advance(
    db_session,
):
    _, action, _ = await arranged(db_session)
    await change(db_session, action, command("start", 1))
    foreign = command(
        "deliver",
        2,
        artifacts=[
            {
                "kind": "content_workflow",
                "title": "外部草稿任务记录",
                "referenceId": "workflow",
            }
        ],
    )
    with pytest.raises(HTTPException):
        await change(
            db_session,
            action,
            foreign,
            artifacts=[
                {
                    "kind": "content_workflow",
                    "reference_id": "workflow",
                    "approval_id": "foreign",
                    "project_id": "p2",
                }
            ],
        )
    await change(
        db_session,
        action,
        command(
            "deliver",
            2,
            artifacts=[
                {
                    "kind": "manual_record",
                    "title": "核实材料范围记录",
                    "notes": "已核对原文与适用范围",
                }
            ],
        ),
    )
    with pytest.raises(HTTPException) as failure:
        await change(
            db_session,
            action,
            command("accept", 3, acceptanceChecks=[{"index": 0, "met": False}]),
        )
    assert failure.value.status_code == 422
    assert (await read_execution(db_session, action, 1))["snapshot"][
        "status"
    ] == "delivered"


@pytest.mark.asyncio
async def test_projection_tampering_is_not_trusted(db_session):
    _, action, _ = await arranged(db_session)
    head = await db_session.scalar(select(ActionExecution))
    head.snapshot = {**head.snapshot, "status": "accepted"}
    with pytest.raises(HTTPException) as failure:
        await read_execution(db_session, action, 1)
    assert failure.value.status_code == 409
    assert (
        await db_session.scalar(select(func.count()).select_from(ActionExecutionEvent))
        == 1
    )
