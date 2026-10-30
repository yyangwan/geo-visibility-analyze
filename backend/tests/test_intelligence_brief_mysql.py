"""Exact content-grant CAS under pre-existing MySQL repeatable-read snapshots."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.intelligence.drafts import authorize_draft, export_brief
from app.intelligence.models import ActionPlan, IntelligenceRun
from tests.test_intelligence_action_revisions_mysql import clean_project
from tests.test_intelligence_drafts import fixture_draft
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401

pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured"
)


@pytest.mark.asyncio
async def test_content_grant_cas_uses_current_reads_after_waiting(
    mysql_sessions, tmp_path, monkeypatch  # noqa: F811 - imported isolated fixture
):
    sessions, _ = mysql_sessions
    project = "ci-brief-" + uuid4().hex
    try:
        async with sessions() as db:
            _, run, plan, _, request = await fixture_draft(
                db, tmp_path, monkeypatch, project
            )
            run_id, action_id = run.id, plan.id
        loaded, both_loaded = 0, asyncio.Event()

        async def approve(index):
            nonlocal loaded
            async with sessions() as db:
                action = await db.get(ActionPlan, action_id)
                run = await db.get(IntelligenceRun, run_id)
                loaded += 1
                if loaded == 2:
                    both_loaded.set()
                await both_loaded.wait()
                try:
                    grant = await authorize_draft(
                        db,
                        action,
                        run,
                        request.model_copy(
                            update={"idempotency_key": f"mysql-content-grant-{index}"}
                        ),
                        "reviewer",
                    )
                    await db.commit()
                    brief = await export_brief(db, grant, action, run)
                    assert (
                        brief["schemaVersion"] == 3
                        and brief["actionRevision"]["revision"] == 2
                    )
                    return "approved"
                except HTTPException as error:
                    await db.rollback()
                    assert error.status_code == 409
                    return "conflict"

        results = await asyncio.wait_for(
            asyncio.gather(approve(1), approve(2)), timeout=25
        )
        assert sorted(results) == ["approved", "conflict"]
    finally:
        await clean_project(sessions, project)
