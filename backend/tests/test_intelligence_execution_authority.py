import pytest
from fastapi import HTTPException

from app.intelligence.semantics import stable_hash
from tests.test_intelligence_action_revisions import task_plan
from tests.test_intelligence_execution import change, command, principal


@pytest.mark.asyncio
async def test_completed_service_artifact_must_match_actual_signed_grant(
    db_session, tmp_path, monkeypatch
):
    from app.intelligence.drafts import authorize_draft, export_brief
    from tests.test_intelligence_drafts import fixture_draft

    _, run, action, _, request = await fixture_draft(db_session, tmp_path, monkeypatch)
    grant = await authorize_draft(db_session, action, run, request, "owner")
    await db_session.commit()
    brief = await export_brief(db_session, grant, action, run)

    def request_for(operation, version, **fields):
        return command(operation, version, **fields).model_copy(
            update={"expected_revision": 2}
        )

    await change(
        db_session,
        action,
        request_for("assign", 0, ownerId="owner", dueAt=None),
        owner={"id": "owner", "name": "内容负责人", "role": "owner"},
    )
    await change(db_session, action, request_for("start", 1))
    receipt = {
        "kind": "content_workflow",
        "reference_id": "workflow",
        "approval_id": grant.id,
        "project_id": "p1",
        "action_id": action.id,
        "revision_id": brief["actionRevision"]["id"],
        "snapshot_hash": stable_hash(brief),
        "status": "succeeded",
        "content_id": "content",
        "observed_at": "2026-10-26T00:00:00Z",
    }
    data = request_for(
        "deliver",
        2,
        artifacts=[
            {
                "kind": "content_workflow",
                "title": "实施说明内容草稿任务",
                "referenceId": "workflow",
            }
        ],
    )
    with pytest.raises(HTTPException) as failure:
        await change(
            db_session,
            action,
            data,
            artifacts=[{**receipt, "revision_id": "other-revision"}],
        )
    assert failure.value.status_code == 409
    value = await change(db_session, action, data, artifacts=[receipt])
    assert (
        value["snapshot"]["delivery"]["artifacts"][0]["verification"]
        == "service_receipt"
    )


@pytest.mark.asyncio
async def test_execution_api_scope_and_signed_request_binding(
    client, db_session, monkeypatch
):
    from app.api.auth import get_current_user
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    _, action, _ = await task_plan(db_session)
    data = command("assign", 0, ownerId="owner", dueAt=None)
    user, _ = principal(
        action, data, owner={"id": "owner", "name": "负责人", "role": "owner"}
    )
    try:
        app.dependency_overrides[get_current_user] = lambda: user
        assert (
            await client.get(
                f"/api/intelligence/actions/{action.id}/execution?revision=1"
            )
        ).json()["version"] == 0
        body = data.model_dump(mode="json", by_alias=True, exclude_unset=True)
        assert (
            await client.post(
                f"/api/intelligence/actions/{action.id}/execution", json=body
            )
        ).status_code == 202
        forged = {**body, "reason": "在授权之后替换请求的执行依据"}
        assert (
            await client.post(
                f"/api/intelligence/actions/{action.id}/execution", json=forged
            )
        ).status_code == 403
        user = {**user, "pid": "other"}
        assert (
            await client.get(
                f"/api/intelligence/actions/{action.id}/execution?revision=1"
            )
        ).status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
