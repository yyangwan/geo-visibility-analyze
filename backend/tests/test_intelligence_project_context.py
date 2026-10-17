import pytest
from fastapi import HTTPException

from app.intelligence.ingestion import prepare_run
from app.intelligence.project_context import (
    ContextRequest,
    context_snapshot,
    receive_context,
)
from tests.test_intelligence_foundation import fixture_audit


@pytest.mark.asyncio
async def test_context_replay_conflicts_order_and_immutable_run(db_session):
    audit = await fixture_audit(db_session)
    old = await prepare_run(db_session, "p1", audit.id)
    data = ContextRequest(
        version=1,
        brands=[
            {"id": "b1", "name": "云衡", "aliases": ["Yunheng"], "is_competitor": True}
        ],
    )
    event = await receive_context(db_session, "p1", data, "actor")
    await db_session.commit()
    assert (await receive_context(db_session, "p1", data, "actor")).id == event.id
    with pytest.raises(HTTPException) as error:
        await receive_context(
            db_session, "p1", ContextRequest(version=1, brands=[]), "actor"
        )
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        await receive_context(
            db_session, "p1", ContextRequest(version=3, brands=[]), "actor"
        )
    assert error.value.status_code == 409
    new = await prepare_run(db_session, "p1", audit.id)
    assert new.id != old.id and new.manifest["brands"][0]["is_competitor"] is True
    assert old.manifest["brand_context"] is None
    assert await context_snapshot(db_session, "p2") is None
    await receive_context(
        db_session, "p1", ContextRequest(version=2, brands=[]), "actor"
    )
    await db_session.commit()
    assert (await context_snapshot(db_session, "p1"))["brands"] == []
    assert (await receive_context(db_session, "p1", data, "actor")).id == event.id
    assert (await context_snapshot(db_session, "p1"))["version"] == 2


@pytest.mark.asyncio
async def test_context_scope_and_viewer_rejection(client, db_session):
    from app.api.auth import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "role": "viewer",
        "sub": "viewer",
    }
    try:
        assert (
            await client.post(
                "/api/intelligence/projects/p1/brand-context",
                json={"version": 1, "brands": []},
            )
        ).status_code == 403
        assert (
            await client.get("/api/intelligence/projects/p2/brand-context")
        ).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
