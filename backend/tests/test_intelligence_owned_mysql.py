"""Real MySQL: a pre-existing repeatable-read snapshot cannot bypass material CAS."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from app.intelligence.blob import LocalBlobStore
from app.intelligence.models import (
    BrandFact,
    OwnedContentAsset,
    ProjectContextEvent,
    ProjectContextHead,
)
from app.intelligence.owned_assets import get_asset, register_asset, verify_asset
from app.intelligence.owned_facts import approve_owned_fact
from app.intelligence.project_context import ContextRequest, receive_context
from app.intelligence.reviews import fact_view
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401
from tests.test_intelligence_owned_assets import (
    TEXT,
    approval,
    registration,
    verification,
)

pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured"
)


@pytest.mark.asyncio
async def test_owned_verification_cas_survives_stale_mysql_snapshot_and_fact_is_audit_independent(
    mysql_sessions,  # noqa: F811 - imported isolated fixture
    tmp_path,
):
    sessions, _ = mysql_sessions
    project = "ci-owned-" + uuid4().hex
    store = LocalBlobStore(tmp_path)
    try:
        async with sessions() as db:
            await receive_context(
                db,
                project,
                ContextRequest(
                    version=1,
                    brands=[{"id": "b1", "name": "云衡", "is_competitor": False}],
                ),
                "owner",
            )
            await db.commit()
            asset = await register_asset(db, project, registration(), "owner", store)
            await db.commit()
            asset_id = asset.id
        ready = [asyncio.Event(), asyncio.Event()]
        bodies = []

        async def writer(index):
            async with sessions() as db:
                stale = await get_asset(
                    db, project, asset_id
                )  # Establish the old MVCC read view.
                body = verification(stale, idempotencyKey=f"mysql-verify-{index}")
                bodies.append((index, body))
                ready[index].set()
                await ready[1 - index].wait()
                try:
                    await verify_asset(db, stale, body, "owner", store)
                    await db.commit()
                    return 202, index
                except HTTPException as error:
                    await db.rollback()
                    return error.status_code, index

        results = await asyncio.gather(writer(0), writer(1))
        assert sorted(code for code, _ in results) == [202, 409]
        winner = next(index for code, index in results if code == 202)
        async with sessions() as db:
            current = await get_asset(db, project, asset_id)
            assert (
                current.verification["version"] == 1
                and len(current.verification["history"]) == 1
            )
            await verify_asset(db, current, dict(bodies)[winner], "owner", store)
            fact = await approve_owned_fact(
                db, current, approval(current), "owner", store
            )
            await db.commit()
            fact_id = fact.id
            assert fact.review_id is None and fact.entity_id is None
            assert fact.source_ref["evidence"]["quote"] in TEXT
            assert (await fact_view(db, fact))["status"] == "valid"
        async with sessions() as stale_session:
            stale = await get_asset(stale_session, project, asset_id)
            async with sessions() as db:
                await receive_context(
                    db,
                    project,
                    ContextRequest(
                        version=2,
                        brands=[{"id": "b1", "name": "云衡", "is_competitor": True}],
                    ),
                    "owner",
                )
                await db.commit()
            with pytest.raises(HTTPException) as denied:
                await verify_asset(
                    stale_session,
                    stale,
                    verification(stale, idempotencyKey="mysql-role-stale"),
                    "owner",
                    store,
                )
            assert denied.value.status_code == 422
            await stale_session.rollback()
        async with sessions() as db:
            assert (await fact_view(db, await db.get(BrandFact, fact_id)))[
                "status"
            ] == "needs_review"
    finally:
        async with sessions() as db, db.begin():
            for model in (
                BrandFact,
                OwnedContentAsset,
                ProjectContextEvent,
                ProjectContextHead,
            ):
                await db.execute(delete(model).where(model.project_id == project))
