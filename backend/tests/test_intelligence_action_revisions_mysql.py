"""Real MySQL serialization of immutable action changes and named approvals."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import Base
from app.intelligence.action_revisions import current_revision, revise_plan
from app.intelligence.actions import approve_plan, plan_view
from app.intelligence.models import ActionApproval, ActionPlan, ActionPlanRevision
from app.models.models import Audit, PlatformResponseRecord
from tests.test_intelligence_action_revisions import (
    approval_request,
    revision_request,
    task_plan,
)
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401

pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured"
)


async def clean_project(sessions, project):
    async with sessions() as db, db.begin():
        audits = select(Audit.id).where(Audit.project_id == project)
        await db.execute(
            PlatformResponseRecord.__table__.delete().where(
                PlatformResponseRecord.audit_id.in_(audits)
            )
        )
        for table in reversed(Base.metadata.sorted_tables):
            if "project_id" in table.c:
                await db.execute(table.delete().where(table.c.project_id == project))


@pytest.mark.asyncio
async def test_mysql_only_one_concurrent_revision_wins_and_approval_cannot_cross_it(
    mysql_sessions,  # noqa: F811 - imported isolated fixture
):
    sessions, _ = mysql_sessions
    project = "ci-rev-" + uuid4().hex
    try:
        async with sessions() as db:
            run, action, _ = await task_plan(db, project)
            run_id, action_id = run.id, action.id
            await approve_plan(db, action, run, approval_request(), "owner", "owner")
            await db.commit()

        async def edit(index):
            async with sessions() as db:
                action = await db.get(ActionPlan, action_id)
                try:
                    row = await revise_plan(
                        db,
                        action,
                        revision_request(
                            title=f"并发编辑的交付版本编号{index}",
                            idempotencyKey=f"mysql-revision-{index}",
                        ),
                        "owner",
                    )
                    await db.commit()
                    return row.revision
                except HTTPException as error:
                    await db.rollback()
                    return error.status_code

        results = await asyncio.gather(edit(1), edit(2))
        assert sorted(results) == [2, 409]
        async with sessions() as db:
            action = await db.get(ActionPlan, action_id)
            revision, run = await current_revision(db, action)
            assert revision.revision == 2 and run.id == run_id
            assert not (await plan_view(db, action, run))["approval_valid"]
            assert (
                len(
                    (
                        await db.scalars(
                            select(ActionPlanRevision).where(
                                ActionPlanRevision.project_id == project
                            )
                        )
                    ).all()
                )
                == 2
            )
            assert (
                len(
                    (
                        await db.scalars(
                            select(ActionApproval).where(
                                ActionApproval.project_id == project
                            )
                        )
                    ).all()
                )
                == 1
            )
            with pytest.raises(HTTPException):
                await approve_plan(
                    db,
                    action,
                    run,
                    approval_request(key="stale-mysql-approval"),
                    "owner",
                    "owner",
                )
            await approve_plan(
                db,
                action,
                run,
                approval_request(2, "mysql-approve-current"),
                "owner",
                "owner",
            )
            await db.commit()
            assert (await plan_view(db, action, run))["approval_valid"]
    finally:
        await clean_project(sessions, project)


@pytest.mark.asyncio
async def test_mysql_revision_request_retry_returns_same_immutable_row(mysql_sessions):  # noqa: F811
    sessions, _ = mysql_sessions
    project = "ci-retry-" + uuid4().hex
    try:
        async with sessions() as db:
            _, action, _ = await task_plan(db, project)
            action_id = action.id

        async def edit():
            async with sessions() as db:
                action = await db.get(ActionPlan, action_id)
                row = await revise_plan(db, action, revision_request(), "owner")
                await db.commit()
                return row.id

        results = await asyncio.gather(edit(), edit())
        assert len(set(results)) == 1
    finally:
        await clean_project(sessions, project)


@pytest.mark.asyncio
async def test_mysql_approval_reads_committed_withdrawal_after_waiting_for_project_lock(
    mysql_sessions, tmp_path  # noqa: F811 - imported isolated fixture
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

    sessions, _ = mysql_sessions
    project = "ci-revoke-" + uuid4().hex
    loaded = asyncio.Event()
    try:
        async with sessions() as db:
            run, action, pack = await task_plan(db, project, own_id="b1")
            store = LocalBlobStore(tmp_path)
            asset = await register_asset(db, project, registration(), "owner", store)
            await verify_asset(db, asset, verification(asset), "owner", store)
            fact = await approve_owned_fact(db, asset, approval(asset), "owner", store)
            await revise_plan(
                db,
                action,
                revision_request(
                    taskPack=pack.model_copy(
                        update={"own_fact_ids": (fact.id,)}
                    ).model_dump(mode="json")
                ),
                "owner",
            )
            action_id, fact_id = action.id, fact.id
            await db.commit()

        async def approve_waiter():
            async with sessions() as db:
                action = await db.get(ActionPlan, action_id)
                _, run = await current_revision(
                    db, action
                )  # Starts a consistent-read snapshot before revocation commits.
                loaded.set()
                try:
                    await approve_plan(
                        db,
                        action,
                        run,
                        approval_request(2, "must-see-revocation"),
                        "owner",
                        "owner",
                    )
                    await db.commit()
                    return 202
                except HTTPException as error:
                    await db.rollback()
                    return error.status_code

        from app.intelligence.models import BrandFact

        async with sessions() as writer:
            fact = await writer.get(BrandFact, fact_id)
            await withdraw_owned_fact(
                writer,
                fact,
                WithdrawRequest(
                    expectedVersion=1,
                    reason="当前事实需撤回后重新核对材料范围",
                    idempotencyKey="mysql-withdraw-fact",
                ),
                "owner",
            )
            waiter = asyncio.create_task(approve_waiter())
            await asyncio.wait_for(loaded.wait(), timeout=10)
            await writer.commit()
        assert await asyncio.wait_for(waiter, timeout=10) == 409
        async with sessions() as db:
            assert not (
                await db.scalars(
                    select(ActionApproval).where(ActionApproval.project_id == project)
                )
            ).all()
            action = await db.get(ActionPlan, action_id)
            _, run = await current_revision(db, action)
            assert (await plan_view(db, action, run))["status"] == "stale"
    finally:
        await clean_project(sessions, project)
