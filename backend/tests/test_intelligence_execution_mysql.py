"""Execution CAS remains current after a MySQL repeatable-read snapshot and wait."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.intelligence.execution import change_execution, read_execution
from app.intelligence.models import ActionPlan
from tests.test_intelligence_action_revisions import task_plan
from tests.test_intelligence_action_revisions_mysql import clean_project
from tests.test_intelligence_execution import command, principal
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401

pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured"
)


@pytest.mark.asyncio
async def test_execution_assignment_serializes_two_old_snapshots(mysql_sessions):  # noqa: F811
    sessions, _ = mysql_sessions
    project = "ci-execution-" + uuid4().hex
    try:
        async with sessions() as db:
            _, action, _ = await task_plan(db, project)
            action_id = action.id
        loaded, both = 0, asyncio.Event()

        async def assign(index):
            nonlocal loaded
            async with sessions() as db:
                action = await db.get(ActionPlan, action_id)
                await read_execution(db, action, 1)
                loaded += 1
                if loaded == 2:
                    both.set()
                await both.wait()
                data = command("assign", 0, ownerId="owner", dueAt=None).model_copy(
                    update={"idempotency_key": f"execution-race-{index}"}
                )
                user, wire = principal(
                    action,
                    data,
                    owner={"id": "owner", "name": "负责人", "role": "owner"},
                )
                try:
                    await change_execution(db, action, data, user, wire)
                    await db.commit()
                    return "saved"
                except HTTPException as error:
                    await db.rollback()
                    assert error.status_code == 409
                    return "conflict"

        results = await asyncio.wait_for(
            asyncio.gather(assign(1), assign(2)), timeout=25
        )
        assert sorted(results) == ["conflict", "saved"]
        async with sessions() as db:
            action = await db.get(ActionPlan, action_id)
            assert (await read_execution(db, action, 1))["version"] == 1
    finally:
        await clean_project(sessions, project)
